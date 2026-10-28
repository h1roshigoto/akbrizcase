"""Orchestration contracts without database I/O; SQL acceptance is separate."""

from dataclasses import replace
from typing import Literal
from uuid import UUID

import pytest
from sqlalchemy.orm import Session
from test_diagnosis import START
from test_diagnosis import tree as tree

from app.modules.chat.actions import ActionRejected, ActionValidator
from app.modules.chat.diagnosis import DiagnosisState
from app.modules.chat.mode_router import ModeRouter
from app.modules.kb.tree_reads import TreeSnapshot
from app.schemas.chat import ChatReply, SelectModeInput
from app.schemas.sources import KnowledgeHit, KnowledgePage, SourceRef


@pytest.mark.parametrize("mode", ["diagnose", "knowledge", "commerce"])
def test_explicit_selection_and_no_implicit_switch(
    mode: Literal["diagnose", "knowledge", "commerce"],
) -> None:
    with Session() as session:
        router = ModeRouter(session)
        initial = DiagnosisState(
            tree_id=UUID(int=1), current_node_key="q", transitions=12
        )
        if mode == "diagnose":
            initial = DiagnosisState(mode="commerce")
        selected = router.route(initial, SelectModeInput(type="select_mode", mode=mode))
        assert selected.state == DiagnosisState(mode=mode)
        assert selected.reply.mode == mode
        assert {
            b.input.mode
            for b in selected.reply.buttons
            if isinstance(b.input, SelectModeInput)
        } == {"diagnose", "knowledge", "commerce"}
        ChatReply.model_validate_json(selected.reply.model_dump_json())


@pytest.mark.parametrize(
    "value",
    [
        "насос",
        "купить руководство",
        "diagnose",
        "commerce",
        "неизвестный запрос",
        "Помогло",
    ],
)
def test_ambiguous_text_only_offers_server_menu(value: str) -> None:
    with Session() as session:
        before = DiagnosisState()
        result = ModeRouter(session).route(before, {"type": "text", "value": value})
        assert result.state == before
        assert result.reply.resolution == "no_match"
        assert len(result.reply.buttons) == 3
        assert all(isinstance(b.input, SelectModeInput) for b in result.reply.buttons)
        assert not result.reply.sources and not result.reply.items


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"type": "select_mode", "mode": "internal"},
        {"type": "button", "option_id": "a", "next": "result"},
        {"type": "button", "option_id": "a", "node_id": "result"},
        {"type": "text", "value": " "},
    ],
)
def test_invalid_input_preserves_progress(event: dict[str, object]) -> None:
    with Session() as session:
        state = DiagnosisState(
            tree_id=UUID(int=1), current_node_key="q", transitions=29
        )
        result = ModeRouter(session).route(state, event)
        assert result.code == "invalid_input" and result.state == state
        assert not result.reply.sources and not result.reply.items


def test_pinned_loader_and_existing_transition_budget(
    tree: TreeSnapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str | None, UUID | None]] = []

    def load(
        session: Session, *, key: str | None = None, tree_id: UUID | None = None
    ) -> TreeSnapshot:
        calls.append((key, tree_id))
        return tree

    monkeypatch.setattr("app.modules.chat.mode_router.load_tree", load)
    with Session() as session:
        router = ModeRouter(session)
        # No source reads when the transition limit requires a safe handoff.
        state = DiagnosisState(
            tree_id=tree.tree_id, current_node_key="root", transitions=30
        )
        result = router.route(state, {"type": "button", "option_id": "a"})
        assert result.state.transitions == 30
        assert result.reply.resolution == "handoff"
        assert calls == [(None, tree.tree_id)]
        reset = router.route(result.state, {"type": "select_mode", "mode": "knowledge"})
        assert reset.state == DiagnosisState(mode="knowledge")
        assert len(calls) == 1


@pytest.mark.parametrize(
    "event",
    [START, {"type": "button", "option_id": "a"}, {"type": "feedback", "useful": True}],
)
def test_inactive_diagnostic_input_does_not_switch_mode(
    event: dict[str, object],
) -> None:
    with Session() as session:
        state = DiagnosisState(mode="commerce")
        result = ModeRouter(session).route(state, event)
        assert result.state == state
        assert result.code == "inactive_input"


@pytest.mark.parametrize(
    "proposed",
    [
        {"type": "execute", "payload": {}},
        {"type": "open_item", "payload": {"item_id": "broken"}},
        {
            "type": "open_item",
            "payload": {"item_id": str(UUID(int=1)), "url": "https://example.invalid"},
        },
        {
            "type": "contact_specialist",
            "payload": {"kind": "support", "url": "javascript:synthetic"},
        },
        {
            "type": "contact_specialist",
            "payload": {
                "kind": "support",
                "comment_draft": "<script>SYNTHETIC</script>",
            },
        },
        {
            "type": "open_request_form",
            "payload": {"kind": "support", "items": [], "comment_draft": "synthetic"},
        },
        {"type": "open_request_form", "payload": {"kind": "commercial", "items": []}},
    ],
)
def test_malformed_actions_rejected_before_reads(proposed: dict[str, object]) -> None:
    with (
        Session() as session,
        pytest.raises(ActionRejected, match="ACTION_UNAVAILABLE"),
    ):
        ActionValidator(session).validate(proposed)


@pytest.mark.parametrize("qty", [0, -1, 1000, True, "1", 1.5])
def test_strict_quantity(qty: object) -> None:
    with Session() as session, pytest.raises(ActionRejected):
        ActionValidator(session).validate(
            {
                "type": "open_request_form",
                "payload": {
                    "kind": "commercial",
                    "items": [{"item_id": str(UUID(int=1)), "qty": qty}],
                },
            }
        )


def test_dto_is_revalidated() -> None:
    event = SelectModeInput.model_construct(type="select_mode", mode="internal")
    with Session() as session:
        state = DiagnosisState()
        result = ModeRouter(session).route(state, event)
        assert result.code == "invalid_input"
        assert result.state == replace(state)


def source() -> SourceRef:
    document, version = UUID(int=70), UUID(int=71)
    return SourceRef(
        section_id=UUID(int=72),
        document_id=document,
        document_version_id=version,
        title="SYNTHETIC",
        section="SYNTHETIC",
        page_from=None,
        page_to=None,
        url=f"/api/v1/documents/{document}/versions/{version}/file",
    )


def test_source_change_between_search_and_action_hands_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Session() as session:
        router = ModeRouter(session)
        monkeypatch.setattr(
            router.knowledge,
            "search",
            lambda query: KnowledgePage(
                items=[KnowledgeHit(excerpt="SYNTHETIC quote", source=source())],
                total=1,
                limit=5,
                offset=0,
            ),
        )

        def reject(*args: object, **kwargs: object) -> None:
            raise ActionRejected("ACTION_UNAVAILABLE")

        monkeypatch.setattr(router.actions, "validate", reject)
        result = router.route(
            DiagnosisState(mode="knowledge"), {"type": "text", "value": "synthetic"}
        )
        assert result.reply.resolution == "handoff"
        assert not result.reply.sources and "SYNTHETIC quote" not in result.reply.text


def test_approved_option_exceeding_wire_label_limit_hands_off(
    tree: TreeSnapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree.content.nodes["root"].options[0].text = "SYNTHETIC " * 30
    monkeypatch.setattr(
        "app.modules.chat.mode_router.load_tree", lambda *args, **kwargs: tree
    )
    with Session() as session:
        router = ModeRouter(session)
        monkeypatch.setattr(router.knowledge, "get_source", lambda identifier: source())
        monkeypatch.setattr(
            router.actions, "validate", lambda proposed, **kwargs: proposed
        )
        state = DiagnosisState(
            tree_id=tree.tree_id, current_node_key="root", transitions=7
        )
        result = router.route(state, {"type": "text", "value": "synthetic"})
        assert result.reply.resolution == "handoff"
        assert result.state.transitions == 7 and result.state.current_node_key == "root"
        assert not result.reply.sources
