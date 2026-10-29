"""Public projection trust boundary; PostgreSQL snapshots are tested separately."""

from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_diagnosis import tree as tree
from test_mode_router import source

from app.modules.chat.diagnosis import DiagnosisState
from app.modules.chat.handoff import GENERAL_SUPPORT_DRAFT
from app.modules.chat.message_state import ResponseEnvelope, StateAfter
from app.modules.chat.models import ChatMessage
from app.modules.chat.projection import ChatReplyProjector, initial_reply, safe_reply
from app.modules.kb.read_service import SourceNotFound
from app.modules.kb.tree_reads import TreeSnapshot
from app.schemas.chat import ContactSpecialistAction
from app.schemas.sources import KnowledgeHit


def envelope(state: DiagnosisState) -> ResponseEnvelope:
    return ResponseEnvelope(
        format_version=1,
        reply=initial_reply(state.mode),
        state_after=StateAfter(**asdict(state), state_version=1),
    )


def test_diagnostic_render_ignores_stored_text_buttons_and_never_steps(
    tree: TreeSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = envelope(DiagnosisState(tree_id=tree.tree_id, current_node_key="root"))
    value.reply.resolution = "question"
    value.reply.text = "SYNTHETIC PRIVATE UNAPPROVED TEXT"
    value.reply.buttons[0].label = "SYNTHETIC PRIVATE LABEL"
    original = deepcopy(value)
    monkeypatch.setattr("app.modules.chat.projection.load_tree", lambda *a, **k: tree)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("projection cannot call step")

    monkeypatch.setattr("app.modules.chat.diagnosis.step", forbidden)
    with Session() as session:
        projector = ChatReplyProjector(session)
        monkeypatch.setattr(projector.knowledge, "get_source", lambda _: source())
        monkeypatch.setattr(projector.actions, "validate", lambda a, **k: a)
        reply = projector.project(value)
    assert reply.text == tree.content.nodes["root"].text
    assert [b.label for b in reply.buttons[:3]] == [
        o.text for o in tree.content.nodes["root"].options
    ]
    assert value == original
    assert "PRIVATE" not in reply.model_dump_json()
    assert (
        reply.actions[-1].model_dump()["payload"]["comment_draft"]
        == GENERAL_SUPPORT_DRAFT
    )


def test_handoff_stays_handoff_without_loading_tree(
    tree: TreeSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = envelope(DiagnosisState(tree_id=tree.tree_id, current_node_key="root"))
    value.reply = safe_reply("diagnose")
    action = value.reply.actions[0]
    assert isinstance(action, ContactSpecialistAction)
    action.payload.comment_draft = "SYNTHETIC POISON"

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("A handoff cannot become a technical answer")

    monkeypatch.setattr("app.modules.chat.projection.load_tree", forbidden)
    with Session() as session:
        assert ChatReplyProjector(session).project(value) == safe_reply("diagnose")


@pytest.mark.parametrize("failure", [SourceNotFound, SQLAlchemyError])
def test_access_failure_hands_off_but_database_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception],
) -> None:
    value = envelope(DiagnosisState(mode="knowledge"))
    value.reply.resolution = "results"
    value.reply.sources = [source()]

    def fail(*args: object, **kwargs: object) -> KnowledgeHit:
        raise failure

    with Session() as session:
        projector = ChatReplyProjector(session)
        monkeypatch.setattr(projector.knowledge, "get_excerpt", fail)
        if failure is SQLAlchemyError:
            with pytest.raises(SQLAlchemyError):
                projector.project(value)
        else:
            assert projector.project(value) == safe_reply("knowledge")


@pytest.mark.parametrize(
    "response", [{}, {"format_version": 999}, {"private": "SYNTHETIC"}]
)
def test_corrupt_message_hides_raw_json(response: dict[str, object]) -> None:
    message = ChatMessage(id=uuid4(), state_version_after=1, response=response)
    with Session() as session:
        assert ChatReplyProjector(session).message(message, "commerce") == safe_reply(
            "commerce"
        )
