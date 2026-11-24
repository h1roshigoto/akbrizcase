"""Three-phase grounded LLM chat orchestration without holding DB locks."""

import hashlib
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db_types import utc_now
from app.core.rate_limits import RateLimited, check_limit
from app.modules.catalog.repository import CatalogRepository
from app.modules.catalog.service import CatalogNotFound, CatalogService
from app.modules.chat.access import ChatExpired, ChatNotFound, require_owner
from app.modules.chat.actions import ActionRejected, ActionValidator
from app.modules.chat.diagnosis import DiagnosisState
from app.modules.chat.handoff import support_handoff
from app.modules.chat.message_state import GroundedMeta, grounded_digest
from app.modules.chat.mode_router import (
    ModeRouter,
    RouterResult,
    catalog_card,
    mode_buttons,
)
from app.modules.chat.models import ChatMessage
from app.modules.kb.access import PUBLIC_VIEWER
from app.modules.kb.read_service import KnowledgeReadService, SourceNotFound
from app.modules.llm import (
    GigaChatClient,
    GroundedCatalogItem,
    GroundedFragment,
    GroundedRequest,
    LLMError,
)
from app.modules.orders.service import OrderRejected, collection_gate
from app.schemas.catalog import CatalogCard, CatalogQuery
from app.schemas.chat import (
    ChatAction,
    ChatMessageCreate,
    ChatMode,
    ChatReply,
    CommercialFormPayload,
    OpenDocumentAction,
    OpenItemAction,
    OpenItemPayload,
    OpenRequestFormAction,
    SuggestedItem,
    TextInput,
)
from app.schemas.sources import KnowledgeQuery, SourceRef


@dataclass(frozen=True)
class PreparedGroundedContext:
    mode: ChatMode
    question: str
    fragments: list[GroundedFragment]
    items: list[GroundedCatalogItem]
    allowed_section_ids: frozenset[UUID]
    allowed_item_ids: frozenset[UUID]


@dataclass(frozen=True)
class GroundedCandidate:
    mode: ChatMode
    model: str
    answer: str
    cited_section_ids: list[UUID]
    cited_item_ids: list[UUID]
    needs_handoff: bool


def _price_label(card: CatalogCard) -> str:
    if card.price_mode == "fixed" and card.price is not None:
        return f"{card.price} {card.currency}"
    return "На заказ"


def prepare_grounded_context(
    session: Session, body: ChatMessageCreate, token: str | None
) -> PreparedGroundedContext | None:
    """Phase 1 short read: load allowed FTS fragments & catalog cards, no locks."""
    if not isinstance(body.input, TextInput):
        return None
    try:
        row = require_owner(session, body.session_id, token, exclusive=False)
    except (ChatNotFound, ChatExpired):
        return None

    if row.mode not in {"knowledge", "commerce"}:
        return None
    previous = session.scalar(
        select(ChatMessage).where(
            ChatMessage.session_id == row.id, ChatMessage.request_id == body.request_id
        )
    )
    if previous is not None:
        return None
    if body.expected_version != row.state_version or row.expires_at <= utc_now():
        return None

    try:
        collection_gate(session)
        check_limit(
            session,
            "message",
            (hashlib.sha256(row.id.bytes).hexdigest(),),
            consume=False,
        )
    except (OrderRejected, RateLimited):
        return None

    question = body.input.value
    knowledge = KnowledgeReadService(session, PUBLIC_VIEWER)
    catalog = CatalogService(CatalogRepository(session))

    kb_hits = knowledge.search(KnowledgeQuery(q=question, limit=5)).items
    fragments = [
        GroundedFragment(
            section_id=hit.source.section_id,
            title=f"{hit.source.title}: {hit.source.section}"[:300],
            text=hit.excerpt[:8000],
        )
        for hit in kb_hits
    ]

    cat_items: list[GroundedCatalogItem] = []
    if row.mode == "commerce":
        page = catalog.items(CatalogQuery(q=question, limit=10))
        for card in page.items:
            cat_items.append(
                GroundedCatalogItem(
                    item_id=card.id,
                    sku=card.sku,
                    name=card.name[:300],
                    price_label=_price_label(card),
                )
            )

    if not fragments and not cat_items:
        return None

    return PreparedGroundedContext(
        mode=cast(ChatMode, row.mode),
        question=question,
        fragments=fragments,
        items=cat_items,
        allowed_section_ids=frozenset(f.section_id for f in fragments),
        allowed_item_ids=frozenset(i.item_id for i in cat_items),
    )


def run_grounded_llm(
    client: GigaChatClient, ctx: PreparedGroundedContext
) -> GroundedCandidate | None:
    """Phase 2 outside DB UoW: invoke GigaChat and enforce citation bounds."""
    try:
        answer, _ = client.generate_grounded(
            GroundedRequest(
                question=ctx.question,
                fragments=ctx.fragments,
                items=ctx.items,
            )
        )
    except LLMError:
        return None

    if answer.needs_handoff:
        return GroundedCandidate(
            mode=ctx.mode,
            model=client.settings.model,
            answer=answer.answer,
            cited_section_ids=[],
            cited_item_ids=[],
            needs_handoff=True,
        )

    if any(sid not in ctx.allowed_section_ids for sid in answer.cited_section_ids):
        return None
    if any(iid not in ctx.allowed_item_ids for iid in answer.cited_item_ids):
        return None

    if ctx.mode == "knowledge" and not 1 <= len(answer.cited_section_ids) <= 5:
        return None
    if ctx.mode == "commerce" and (
        len(answer.cited_item_ids) > 10
        or len(answer.cited_section_ids) > 5
        or (not answer.cited_item_ids and not answer.cited_section_ids)
    ):
        return None

    return GroundedCandidate(
        mode=ctx.mode,
        model=client.settings.model,
        answer=answer.answer,
        cited_section_ids=answer.cited_section_ids,
        cited_item_ids=answer.cited_item_ids,
        needs_handoff=False,
    )


def materialize_grounded_reply(
    session: Session, state: DiagnosisState, candidate: GroundedCandidate
) -> tuple[RouterResult, GroundedMeta | None]:
    """Phase 3 inside write UoW: live ACL/catalog recheck before committing reply."""
    router = ModeRouter(session)
    if candidate.needs_handoff:
        return router._handoff(state, "llm_handoff"), None

    knowledge = KnowledgeReadService(session, PUBLIC_VIEWER)
    catalog = CatalogService(CatalogRepository(session))
    actions_validator = ActionValidator(session)

    try:
        sources: list[SourceRef] = []
        doc_actions: list[ChatAction] = []
        for sid in candidate.cited_section_ids:
            hit = knowledge.get_excerpt(sid)
            sources.append(hit.source)
            doc_actions.append(
                actions_validator.validate(
                    OpenDocumentAction(type="open_document", payload=hit.source)
                )
            )

        cards: list[CatalogCard] = []
        for iid in candidate.cited_item_ids:
            cards.append(catalog_card(catalog.item(iid, None)))

        selected = [SuggestedItem(item_id=c.id, qty=1) for c in cards]
        item_actions: list[ChatAction] = []
        if selected:
            proposed: list[ChatAction] = [
                OpenItemAction(
                    type="open_item", payload=OpenItemPayload(item_id=item.item_id)
                )
                for item in selected
            ]
            proposed.append(
                OpenRequestFormAction(
                    type="open_request_form",
                    payload=CommercialFormPayload(kind="commercial", items=selected),
                )
            )
            item_actions = [
                actions_validator.validate(action, model_id=None) for action in proposed
            ]
    except (SourceNotFound, CatalogNotFound, ActionRejected):
        return router._handoff(state, "source_unavailable"), None

    actions = (
        doc_actions + item_actions + [support_handoff()]
        if candidate.mode == "knowledge"
        else item_actions + doc_actions + [support_handoff()]
    )
    reply = ChatReply(
        mode=candidate.mode,
        text=candidate.answer,
        buttons=mode_buttons(),
        sources=sources,
        items=cards,
        actions=actions,
        resolution="results",
    )
    meta = GroundedMeta(
        model=candidate.model,
        content_digest=grounded_digest(
            candidate.mode,
            reply.text,
            [s.section_id for s in sources],
            [c.id for c in cards],
        ),
    )
    return RouterResult(state, reply, "ok"), meta
