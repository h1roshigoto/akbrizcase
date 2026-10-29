"""The sole public reply boundary for committed POST, replay and history.

The caller owns a fresh, consistent PostgreSQL read transaction. Stored DTOs
identify logical results; they never authorize text, cards, labels or actions.
No router, transition, FTS search, write, flush or commit runs during projection.
"""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.catalog.repository import CatalogRepository
from app.modules.catalog.service import CatalogNotFound, CatalogService
from app.modules.chat.actions import ActionRejected, ActionValidator
from app.modules.chat.diagnosis import HANDOFF_TEXT, current_reply
from app.modules.chat.handoff import support_handoff
from app.modules.chat.message_state import (
    ResponseEnvelope,
    StoredStateUnavailable,
    decode_response,
    restore_state,
)
from app.modules.chat.mode_router import catalog_card, mode_buttons
from app.modules.chat.models import ChatMessage, ChatSession
from app.modules.kb.access import PUBLIC_VIEWER
from app.modules.kb.read_service import KnowledgeReadService, SourceNotFound
from app.modules.kb.tree_reads import load_tree
from app.schemas.catalog import CatalogCard
from app.schemas.chat import (
    ChatAction,
    ChatButton,
    ChatMode,
    ChatReply,
    CommercialFormPayload,
    OpenDocumentAction,
    OpenItemAction,
    OpenItemPayload,
    OpenRequestFormAction,
    SuggestedItem,
)
from app.schemas.sources import SourceRef


def begin_projection_read(session: Session) -> None:
    """First statement of a new outer UoW, including after POST's COMMIT.

    One MVCC snapshot covers ownership, all exchanges and all dependencies.
    Retain the existing session FOR SHARE lock (incompatible with READ ONLY).
    Content lifecycle writers never wait for projection's content row locks.
    """
    session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))


def safe_reply(mode: ChatMode) -> ChatReply:
    return ChatReply(
        mode=mode,
        text=HANDOFF_TEXT,
        buttons=[],
        sources=[],
        items=[],
        actions=[support_handoff()],
        resolution="handoff",
    )


def initial_reply(mode: ChatMode) -> ChatReply:
    return ChatReply(
        mode=mode,
        text=(
            "Выберите режим или введите запрос. "
            "Для диагностики выберите модель и сценарий."
        ),
        buttons=mode_buttons(),
        sources=[],
        items=[],
        actions=[support_handoff()],
        resolution="no_match",
    )


class ChatReplyProjector:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.knowledge = KnowledgeReadService(session, PUBLIC_VIEWER)
        self.catalog = CatalogService(CatalogRepository(session))
        self.actions = ActionValidator(session)

    def message(
        self,
        message: ChatMessage,
        fallback_mode: ChatMode,
        *,
        current: ChatSession | None = None,
    ) -> ChatReply:
        with self.session.no_autoflush:
            try:
                envelope = decode_response(message)
            except StoredStateUnavailable:
                return safe_reply(fallback_mode)
            if current is not None:
                if message.state_version_after > current.state_version:
                    return safe_reply(fallback_mode)
                if message.state_version_after == current.state_version:
                    try:
                        if (
                            restore_state(self.session, current)
                            != envelope.state_after.diagnosis()
                        ):
                            return safe_reply(fallback_mode)
                    except StoredStateUnavailable:
                        return safe_reply(fallback_mode)
            return self.project(envelope)

    def project(
        self, envelope: ResponseEnvelope, *, model_id: UUID | None = None
    ) -> ChatReply:
        """model_id, if supplied, must be authoritative server context, never JSONB."""
        # Reparse even DTO instances; never mutate the envelope or session state.
        try:
            envelope = ResponseEnvelope.model_validate(envelope.model_dump())
        except ValidationError:
            return safe_reply("diagnose")
        mode = envelope.state_after.mode
        with self.session.no_autoflush:
            try:
                if envelope.reply.resolution == "handoff":
                    return safe_reply(mode)
                if mode == "diagnose" and envelope.state_after.tree_id is not None:
                    return self._diagnose(envelope)
                if envelope.reply.resolution == "no_match":
                    return self._no_match(mode)
                if mode == "knowledge":
                    return self._knowledge(envelope)
                if mode == "commerce":
                    return self._commerce(envelope, model_id)
            except (SourceNotFound, ActionRejected, ValidationError):
                # Hide the entire technical reply on authorization/content errors.
                # Infrastructure failures propagate to HTTP's existing 503.
                return safe_reply(mode)
        return safe_reply(mode)

    def _no_match(self, mode: ChatMode) -> ChatReply:
        reply = initial_reply(mode)
        if mode == "knowledge":
            reply.text = (
                "В опубликованных материалах подтверждения не найдено. "
                "Обратитесь к специалисту."
            )
        elif mode == "commerce":
            reply.text = (
                "Доступные товары и услуги не найдены. Обратитесь к специалисту."
            )
        return reply

    def _document_actions(self, sources: list[SourceRef]) -> list[ChatAction]:
        return [
            self.actions.validate(OpenDocumentAction(type="open_document", payload=s))
            for s in sources
        ]

    def _item_actions(
        self, selected: list[SuggestedItem], model_id: UUID | None
    ) -> list[ChatAction]:
        if not selected:
            return []
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
        return [self.actions.validate(a, model_id=model_id) for a in proposed]

    def _knowledge(self, envelope: ResponseEnvelope) -> ChatReply:
        saved = envelope.reply
        if saved.resolution != "results" or not 1 <= len(saved.sources) <= 5:
            return safe_reply("knowledge")
        hits = [self.knowledge.get_excerpt(s.section_id) for s in saved.sources]
        for old, hit in zip(saved.sources, hits, strict=True):
            if (old.document_id, old.document_version_id) != (
                hit.source.document_id,
                hit.source.document_version_id,
            ):
                return safe_reply("knowledge")
        quote = "Цитаты из опубликованных материалов (не диагноз):\n\n" + "\n\n".join(
            f"[{index}] {hit.excerpt}" for index, hit in enumerate(hits, start=1)
        )
        # An adjacent valid SourceRef cannot bless an arbitrary stored paragraph.
        if quote != saved.text:
            return safe_reply("knowledge")
        sources = [hit.source for hit in hits]
        return ChatReply(
            mode="knowledge",
            text=quote,
            sources=sources,
            items=[],
            buttons=mode_buttons(),
            actions=self._document_actions(sources),
            resolution="results",
        )

    def _commerce(self, envelope: ResponseEnvelope, model_id: UUID | None) -> ChatReply:
        saved = envelope.reply
        if saved.resolution != "results" or len(saved.items) > 10:
            return safe_reply("commerce")
        cards = []
        for identifier in dict.fromkeys(item.id for item in saved.items):
            try:
                # HTTP commerce has no persisted authoritative model context.
                # Never adopt model_id or confirmed from a saved card's JSONB.
                cards.append(catalog_card(self.catalog.item(identifier, model_id)))
            except CatalogNotFound:
                continue
        if not cards:
            return self._no_match("commerce")
        selected = [SuggestedItem(item_id=c.id, qty=1) for c in cards]
        return ChatReply(
            mode="commerce",
            text=(
                "Опубликованные товары и услуги. "
                "Неподтверждённая применимость требует уточнения у специалиста."
            ),
            buttons=mode_buttons(),
            sources=[],
            items=cards,
            actions=self._item_actions(selected, model_id) + [support_handoff()],
            resolution="results",
        )

    def _diagnose(self, envelope: ResponseEnvelope) -> ChatReply:
        state = envelope.state_after.diagnosis()
        tree = load_tree(self.session, tree_id=state.tree_id)
        rendered = current_reply(state, tree)
        if rendered.resolution == "handoff" or tree is None:
            return safe_reply("diagnose")
        # Node, options, sources, quantities and result come from the exact pin.
        sources = [self.knowledge.get_source(s) for s in rendered.source_ids]
        cards: list[CatalogCard] = []
        selected = []
        for recommendation in rendered.recommendations:
            try:
                card = catalog_card(
                    self.catalog.item(recommendation.item_id, tree.model_id)
                )
            except CatalogNotFound:
                continue
            if card.applicability is None or card.applicability.status != "confirmed":
                continue
            cards.append(card)
            selected.append(SuggestedItem(item_id=card.id, qty=recommendation.qty))
        return ChatReply(
            mode="diagnose",
            text=rendered.text,
            buttons=[
                ChatButton(id=f"diagnosis_{i}", label=b.label, input=b.input)
                for i, b in enumerate(rendered.buttons)
            ]
            + mode_buttons(),
            sources=sources,
            items=cards,
            actions=self._document_actions(sources)
            + self._item_actions(selected, tree.model_id)
            + [support_handoff()],
            resolution=rendered.resolution,
        )
