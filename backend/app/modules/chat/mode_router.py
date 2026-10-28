"""Read-only mode orchestration for MVP-033; deliberately absent from HTTP.

The caller supplies trusted DiagnosisState (including the transition count),
an optional equipment context and untrusted input. It receives fresh state and
a ChatReply, and owns persistence/locking/idempotency in its outer UoW. This
module never writes messages, orders, session versions or historical responses.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.modules.catalog.repository import CatalogRepository
from app.modules.catalog.service import CatalogNotFound, CatalogService
from app.modules.chat.actions import ActionRejected, ActionValidator
from app.modules.chat.diagnosis import HANDOFF_TEXT, DiagnosisState, step
from app.modules.kb.access import PUBLIC_VIEWER
from app.modules.kb.read_service import KnowledgeReadService, SourceNotFound
from app.modules.kb.tree_reads import load_tree, published_scenarios
from app.schemas.catalog import CatalogCard, CatalogQuery
from app.schemas.chat import (
    ChatAction,
    ChatButton,
    ChatInput,
    ChatMode,
    ChatReply,
    CommercialFormPayload,
    ContactSpecialistAction,
    OpenDocumentAction,
    OpenItemAction,
    OpenItemPayload,
    OpenRequestFormAction,
    SelectModeInput,
    SpecialistPayload,
    StartDiagnosisInput,
    SuggestedItem,
    TextInput,
)
from app.schemas.sources import KnowledgeQuery

_INPUT = TypeAdapter[ChatInput](ChatInput)
MODES: tuple[tuple[ChatMode, str], ...] = (
    ("diagnose", "Диагностика"),
    ("knowledge", "Документация"),
    ("commerce", "Товары и услуги"),
)


@dataclass(frozen=True)
class RouterResult:
    state: DiagnosisState
    reply: ChatReply
    code: str = "ok"


def mode_buttons() -> list[ChatButton]:
    return [
        ChatButton(
            id=f"mode_{mode}",
            label=label,
            input=SelectModeInput(type="select_mode", mode=mode),
        )
        for mode, label in MODES
    ]


def specialist() -> ContactSpecialistAction:
    return ContactSpecialistAction(
        type="contact_specialist", payload=SpecialistPayload(kind="support")
    )


def catalog_card(item: CatalogCard) -> CatalogCard:
    # CatalogService.item returns detail; ChatReply must contain the card only.
    return CatalogCard.model_validate(
        {name: getattr(item, name) for name in CatalogCard.model_fields}
    )


class ModeRouter:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.knowledge = KnowledgeReadService(session, PUBLIC_VIEWER)
        self.catalog = CatalogService(CatalogRepository(session))
        self.actions = ActionValidator(session)

    def route(
        self,
        state: DiagnosisState,
        input: ChatInput | Mapping[str, object],
        *,
        model_id: UUID | None = None,
    ) -> RouterResult:
        raw = (
            dict(input)
            if isinstance(input, Mapping)
            else input.model_dump(warnings=False)
        )
        with self.session.no_autoflush:
            try:
                event = _INPUT.validate_python(raw)
            except ValidationError:
                return self._menu(
                    state,
                    "Некорректный ввод. Выберите вариант.",
                    model_id,
                    "invalid_input",
                )
            if isinstance(event, SelectModeInput):
                if event.mode != state.mode:
                    state = step(state, event, None).state
                elif state.tree_id is not None:
                    return self._diagnose(state, event)
                return self._menu(
                    state,
                    "Режим выбран. Выберите сценарий или введите запрос.",
                    model_id,
                )
            if state.mode == "diagnose":
                if state.tree_id is not None or isinstance(event, StartDiagnosisInput):
                    return self._diagnose(state, event)
                return self._menu(
                    state,
                    "Выберите режим или опубликованный сценарий для модели.",
                    model_id,
                )
            if not isinstance(event, TextInput):
                return self._menu(
                    state,
                    "Выберите режим или введите запрос.",
                    model_id,
                    "inactive_input",
                )
            if state.mode == "knowledge":
                try:
                    return self._knowledge(state, event.value)
                except ActionRejected:
                    return self._handoff(state, "source_unavailable")
            try:
                return self._commerce(state, event.value, model_id)
            except ActionRejected:
                return self._handoff(state, "item_unavailable")

    def _handoff(self, state: DiagnosisState, code: str) -> RouterResult:
        return RouterResult(
            replace(state, outcome="handoff"),
            ChatReply(
                mode=state.mode,
                text=HANDOFF_TEXT,
                buttons=mode_buttons(),
                sources=[],
                items=[],
                actions=[specialist()],
                resolution="handoff",
            ),
            code,
        )

    def _menu(
        self,
        state: DiagnosisState,
        text: str,
        model_id: UUID | None,
        code: str = "ok",
    ) -> RouterResult:
        buttons = mode_buttons()
        if state.mode == "diagnose" and state.tree_id is None and model_id is not None:
            buttons += [
                ChatButton(
                    id=f"scenario_{index}",
                    label=choice.title,
                    input=StartDiagnosisInput(
                        type="start_diagnosis",
                        model_id=choice.model_id,
                        tree_key=choice.key,
                    ),
                )
                for index, choice in enumerate(
                    published_scenarios(self.session, model_id)
                )
            ]
        return RouterResult(
            state,
            ChatReply(
                mode=state.mode,
                text=text,
                buttons=buttons,
                sources=[],
                items=[],
                actions=[specialist()],
                resolution="no_match",
            ),
            code,
        )

    def _knowledge(self, state: DiagnosisState, text: str) -> RouterResult:
        page = self.knowledge.search(KnowledgeQuery(q=text, limit=5))
        if not page.items:
            return self._menu(
                state,
                "В опубликованных материалах подтверждения не найдено. "
                "Обратитесь к специалисту или выберите другой режим.",
                None,
            )
        # Each numbered verbatim excerpt corresponds to the same-index SourceRef.
        # No paraphrase, technical conclusion, or repair plan is generated.
        reply = ChatReply(
            mode=state.mode,
            text="Цитаты из опубликованных материалов (не диагноз):\n\n"
            + "\n\n".join(
                f"[{index}] {hit.excerpt}"
                for index, hit in enumerate(page.items, start=1)
            ),
            sources=[hit.source for hit in page.items],
            items=[],
            buttons=mode_buttons(),
            actions=[
                self.actions.validate(
                    OpenDocumentAction(
                        type="open_document",
                        payload=hit.source,
                    )
                )
                for hit in page.items
            ],
            resolution="results",
        )
        return RouterResult(state, reply)

    def _commerce(
        self,
        state: DiagnosisState,
        text: str,
        model_id: UUID | None,
    ) -> RouterResult:
        # Equipment context is not a confirmed-only filter: name/SKU matches
        # remain visible with explicit unknown applicability. The existing item
        # reader computes public evidence and handles unavailable models.
        page = self.catalog.items(CatalogQuery(q=text, limit=10))
        cards = []
        for item in page.items:
            try:
                cards.append(catalog_card(self.catalog.item(item.id, model_id)))
            except CatalogNotFound:
                continue
        if not cards:
            return self._menu(
                state,
                "Опубликованные товары и услуги не найдены. "
                "Уточните название или артикул либо обратитесь к специалисту.",
                model_id,
            )
        return RouterResult(
            state,
            ChatReply(
                mode=state.mode,
                text="Опубликованные товары и услуги. "
                "Неподтверждённая применимость требует уточнения у специалиста.",
                buttons=mode_buttons(),
                sources=[],
                items=cards,
                actions=self._item_actions(
                    [SuggestedItem(item_id=c.id, qty=1) for c in cards], model_id
                )
                + [specialist()],
                resolution="results",
            ),
        )

    def _item_actions(
        self,
        items: list[SuggestedItem],
        model_id: UUID | None,
    ) -> list[ChatAction]:
        if not items:
            return []
        proposed: list[ChatAction] = [
            OpenItemAction(
                type="open_item",
                payload=OpenItemPayload(item_id=item.item_id),
            )
            for item in items
        ]
        proposed.append(
            OpenRequestFormAction(
                type="open_request_form",
                payload=CommercialFormPayload(kind="commercial", items=items),
            )
        )
        return [self.actions.validate(action, model_id=model_id) for action in proposed]

    def _diagnose(self, state: DiagnosisState, event: ChatInput) -> RouterResult:
        tree = (
            load_tree(self.session, key=event.tree_key)
            if isinstance(event, StartDiagnosisInput)
            else load_tree(self.session, tree_id=state.tree_id)
        )
        result = step(state, event, tree)
        model_id = tree.model_id if tree is not None else None
        try:
            sources = [self.knowledge.get_source(s) for s in result.reply.source_ids]
            cards, selected = [], []
            for recommendation in result.reply.recommendations:
                try:
                    card = catalog_card(
                        self.catalog.item(recommendation.item_id, model_id)
                    )
                except CatalogNotFound:
                    continue
                if (
                    card.applicability is None
                    or card.applicability.status != "confirmed"
                ):
                    continue
                cards.append(card)
                selected.append(SuggestedItem(item_id=card.id, qty=recommendation.qty))
            actions: list[ChatAction] = [*result.reply.actions]
            actions += [
                self.actions.validate(
                    OpenDocumentAction(
                        type="open_document",
                        payload=source,
                    )
                )
                for source in sources
            ]
            actions += self._item_actions(selected, model_id)
            reply = ChatReply(
                mode=result.reply.mode,
                text=result.reply.text,
                buttons=[
                    ChatButton(id=f"diagnosis_{index}", label=b.label, input=b.input)
                    for index, b in enumerate(result.reply.buttons)
                ]
                + mode_buttons(),
                sources=sources,
                items=cards,
                actions=actions,
                resolution=result.reply.resolution,
            )
            return RouterResult(result.state, reply, result.code)
        except (SourceNotFound, ActionRejected, ValidationError):
            # A dependency changed between public reads: never expose a partial
            # technical reply and never accept the edge/feedback on that input.
            return self._handoff(state, "tree_unavailable")
