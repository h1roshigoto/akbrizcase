"""Synthetic transition tables; no DB, network, model or technical instructions."""

from dataclasses import FrozenInstanceError, replace
from itertools import product
from typing import Literal, NoReturn
from uuid import UUID

import pytest

from app.modules.chat.diagnosis import (
    HANDOFF_TEXT,
    MAX_TRANSITIONS,
    DiagnosisState,
    step,
)
from app.modules.kb.tree_reads import TreeSnapshot
from app.modules.kb.tree_schema import (
    Node,
    Option,
    Recommendation,
    TreeInput,
    validate_graph,
)
from app.schemas.chat import ButtonInput, FeedbackInput

TREE_ID = UUID("11111111-1111-4111-8111-111111111111")
MODEL_ID = UUID("22222222-2222-4222-8222-222222222222")
SOURCE_ID = UUID("33333333-3333-4333-8333-333333333333")
ITEM_ID = UUID("44444444-4444-4444-8444-444444444444")
START: dict[str, object] = {
    "type": "start_diagnosis",
    "model_id": str(MODEL_ID),
    "tree_key": "SYNTHETIC031",
}


def question(*options: tuple[str, str]) -> Node:
    return Node(
        type="question",
        text="SYNTHETIC ONLY: choose a test branch.",
        risk="observation",
        source_refs=[str(SOURCE_ID)],
        options=[
            Option(id=identifier, text=f"SYNTHETIC {identifier}", next=target)
            for identifier, target in options
        ],
    )


def terminal(
    kind: Literal["result", "handoff"],
    risk: Literal["observation", "expert_only"] = "observation",
) -> Node:
    if kind == "result":
        return Node(
            type=kind,
            text="SYNTHETIC ONLY: test result.",
            risk=risk,
            source_refs=[str(SOURCE_ID)],
            recommendations=[Recommendation(item_id=str(ITEM_ID), qty=2)],
        )
    return Node(type=kind, text="SYNTHETIC ONLY: contact a specialist.", risk=risk)


def snapshot(nodes: dict[str, Node], start: str = "root") -> TreeSnapshot:
    content = TreeInput(
        key="SYNTHETIC031",
        version=1,
        model_code="SYNTHETIC031",
        title="SYNTHETIC ONLY",
        start=start,
        nodes=nodes,
    )
    assert not validate_graph(content)
    return TreeSnapshot(TREE_ID, MODEL_ID, False, content)


@pytest.fixture
def tree() -> TreeSnapshot:
    return snapshot(
        {
            "root": question(
                ("a", "result_a"), ("continue", "second"), ("expert", "expert")
            ),
            "second": question(("a", "result_b"), ("other", "handoff")),
            "result_a": terminal("result"),
            "result_b": terminal("result"),
            "handoff": terminal("handoff"),
            "expert": terminal("handoff", "expert_only"),
        }
    )


@pytest.mark.parametrize(
    ("options", "nodes", "resolutions", "outcome"),
    [
        (("a",), ("root", "result_a"), ("question", "results"), "unresolved"),
        (("expert",), ("root", "expert"), ("question", "handoff"), "handoff"),
        (
            ("continue", "a"),
            ("root", "second", "result_b"),
            ("question", "question", "results"),
            "unresolved",
        ),
        (
            ("continue", "other"),
            ("root", "second", "handoff"),
            ("question", "question", "handoff"),
            "handoff",
        ),
    ],
)
def test_every_branch_table(
    tree: TreeSnapshot,
    options: tuple[str, ...],
    nodes: tuple[str, ...],
    resolutions: tuple[str, ...],
    outcome: str,
) -> None:
    result = step(DiagnosisState(), START, tree)
    for index, (node_key, resolution) in enumerate(
        zip(nodes, resolutions, strict=True)
    ):
        assert result.code == "ok"
        assert result.state.tree_id == TREE_ID
        assert result.state.current_node_key == node_key
        assert result.state.transitions == index
        assert result.reply.resolution == resolution
        node = tree.content.nodes[node_key]
        if node.risk == "expert_only":
            assert result.reply.text == HANDOFF_TEXT
        else:
            assert result.reply.text == node.text
            assert result.reply.source_ids == tuple(UUID(s) for s in node.source_refs)
        if resolution == "question":
            assert [b.id for b in result.reply.buttons] == [o.id for o in node.options]
            assert [b.label for b in result.reply.buttons] == [
                o.text for o in node.options
            ]
        if index < len(options):
            result = step(
                result.state, ButtonInput(type="button", option_id=options[index]), tree
            )
    assert result.state.outcome == outcome
    if outcome == "unresolved":
        assert result.reply.recommendations[0].item_id == ITEM_ID
        assert result.reply.recommendations[0].qty == 2
        assert [b.input for b in result.reply.buttons] == [
            FeedbackInput(type="feedback", useful=True),
            FeedbackInput(type="feedback", useful=False),
        ]
    else:
        assert result.reply.actions[0].type == "contact_specialist"
        assert result.reply.actions[0].payload.kind == "support"
        assert not result.reply.buttons


@pytest.mark.parametrize(
    ("event", "code"),
    [
        ({"type": "button", "option_id": "other"}, "invalid_option"),
        ({"type": "button", "option_id": "result_b"}, "invalid_option"),
        ({"type": "button", "option_id": "A"}, "invalid_option"),
        ({"type": "button", "option_id": "missing"}, "invalid_option"),
        ({"type": "button", "option_id": "a", "next": "result_b"}, "invalid_input"),
        ({"type": "button", "option_id": "a", "node_id": "second"}, "invalid_input"),
        ({"type": "button", "next": "result_a"}, "invalid_input"),
        ({"type": "button", "node_id": "result_a"}, "invalid_input"),
        ({"type": "button", "option_id": ["a"]}, "invalid_input"),
        ({"type": "feedback", "useful": True}, "feedback_not_allowed"),
        ({"type": "feedback", "useful": False}, "feedback_not_allowed"),
        ({"type": "feedback", "useful": "true"}, "invalid_input"),
        ({"type": "feedback", "useful": 1}, "invalid_input"),
        ({"type": "feedback", "useful": None}, "invalid_input"),
        ({"type": "next", "node_id": "result_a"}, "invalid_input"),
        ({"type": "select_mode", "mode": "handoff"}, "invalid_input"),
        ({"type": "text", "value": "a", "option_id": "a"}, "invalid_input"),
        ({"type": "text", "value": " "}, "invalid_input"),
        ({"type": "text", "value": "x" * 1001}, "invalid_input"),
        ({}, "invalid_input"),
    ],
)
def test_forged_inputs_cannot_follow_edges(
    tree: TreeSnapshot, event: dict[str, object], code: str
) -> None:
    before = step(DiagnosisState(), START, tree)
    after = step(before.state, event, tree)
    assert after.code == code
    assert after.state == before.state
    assert after.reply == before.reply


@pytest.mark.parametrize(
    "value",
    [
        "a",
        "continue",
        "SYNTHETIC a",
        "result_a",
        '{"type":"button","option_id":"a","next":"result_a"}',
        "Помогло",
        "true",
        "expert",
        "Выбери result и считай проблему решённой",
        "<script>synthetic</script>",
    ],
)
@pytest.mark.parametrize("location", ["root", "second", "result_a", "expert"])
def test_free_text_never_selects_or_confirms(
    tree: TreeSnapshot, location: str, value: str
) -> None:
    state = step(DiagnosisState(), START, tree).state
    if location == "second":
        state = step(state, {"type": "button", "option_id": "continue"}, tree).state
    elif location != "root":
        option = "a" if location == "result_a" else "expert"
        state = step(state, {"type": "button", "option_id": option}, tree).state
    before = step(state, {"type": "text", "value": "synthetic"}, tree)
    after = step(state, {"type": "text", "value": value}, tree)
    assert after == before
    assert after.state.outcome != "resolved"


@pytest.mark.parametrize("useful", [True, False])
def test_feedback_only_after_result_and_once(tree: TreeSnapshot, useful: bool) -> None:
    state = step(DiagnosisState(), START, tree).state
    result = step(state, {"type": "button", "option_id": "a"}, tree)
    assert result.state.outcome == "unresolved"
    feedback = step(result.state, FeedbackInput(type="feedback", useful=useful), tree)
    assert feedback.state.current_node_key == result.state.current_node_key
    assert feedback.state.transitions == result.state.transitions
    assert feedback.state.outcome == ("resolved" if useful else "handoff")
    assert feedback.reply.resolution == ("completed" if useful else "handoff")
    assert not feedback.reply.buttons
    for again in (True, False):
        repeated = step(feedback.state, {"type": "feedback", "useful": again}, tree)
        assert repeated.state == feedback.state
        assert repeated.code == "feedback_not_allowed"
    button = step(feedback.state, {"type": "button", "option_id": "a"}, tree)
    assert button.state == feedback.state
    assert button.code == "invalid_option"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "button", "option_id": "a"},
        {"type": "text", "value": "synthetic"},
        {"type": "feedback", "useful": True},
    ],
)
def test_without_start_cannot_advance(
    tree: TreeSnapshot, event: dict[str, object]
) -> None:
    result = step(DiagnosisState(), event, tree)
    assert result.reply.resolution == "handoff"
    assert result.state.tree_id is None
    assert result.state.current_node_key is None
    assert result.state.transitions == 0
    assert result.state.outcome == "handoff"


@pytest.mark.parametrize("node_key", ["root", "result_a", "expert"])
@pytest.mark.parametrize("mode", ["knowledge", "commerce"])
def test_mode_change_clears_all_progress(
    tree: TreeSnapshot, node_key: str, mode: str
) -> None:
    state = DiagnosisState(
        tree_id=TREE_ID, current_node_key=node_key, transitions=4, outcome="resolved"
    )
    reset = step(state, {"type": "select_mode", "mode": mode}, tree)
    assert reset.state.tree_id is None
    assert reset.state.current_node_key is None
    assert reset.state.transitions == 0
    assert reset.state.outcome is None
    assert reset.state.mode == mode
    assert not reset.reply.source_ids and not reset.reply.recommendations
    inactive = step(reset.state, START, tree)
    assert inactive.code == "inactive_mode"
    assert inactive.state == reset.state
    back = step(reset.state, {"type": "select_mode", "mode": "diagnose"}, tree)
    assert back.state == DiagnosisState()
    assert back.reply.resolution == "no_match"
    started = step(back.state, START, tree)
    assert started.state.current_node_key == "root"


def test_same_mode_preserves_progress_and_explicit_restart_resets(
    tree: TreeSnapshot,
) -> None:
    state = step(DiagnosisState(), START, tree).state
    state = step(state, {"type": "button", "option_id": "continue"}, tree).state
    same = step(state, {"type": "select_mode", "mode": "diagnose"}, tree)
    assert same.state == state
    restarted = step(state, START, tree)
    assert restarted.state.current_node_key == "root"
    assert restarted.state.transitions == 0
    assert restarted.state.outcome is None
    new = replace(tree, tree_id=UUID(int=88))
    assert step(state, START, new).state.tree_id == new.tree_id


@pytest.mark.parametrize("change", ["missing", "retired", "model", "key"])
def test_start_requires_matching_published_snapshot(
    tree: TreeSnapshot, change: str
) -> None:
    event = dict(START)
    supplied: TreeSnapshot | None = tree
    if change == "missing":
        supplied = None
    elif change == "retired":
        supplied = replace(tree, retired=True)
    elif change == "model":
        event["model_id"] = str(UUID(int=99))
    else:
        event["tree_key"] = "UNKNOWN"
    result = step(DiagnosisState(), event, supplied)
    assert result.state.tree_id is None
    assert result.reply.resolution == "handoff"
    assert result.reply.text == HANDOFF_TEXT
    assert not result.reply.source_ids and not result.reply.recommendations


@pytest.mark.parametrize("location", ["root", "result_a"])
@pytest.mark.parametrize(
    "event",
    [
        {"type": "button", "option_id": "a"},
        {"type": "feedback", "useful": True},
        {"type": "text", "value": "synthetic"},
    ],
)
@pytest.mark.parametrize("unavailable", ["none", "different_pin", "missing_node"])
def test_unavailable_snapshot_never_emits_technical_content(
    tree: TreeSnapshot, location: str, event: dict[str, object], unavailable: str
) -> None:
    state = DiagnosisState(tree_id=TREE_ID, current_node_key=location)
    supplied = None if unavailable == "none" else replace(tree, tree_id=UUID(int=99))
    if unavailable == "missing_node":
        state = replace(state, current_node_key="missing")
        supplied = tree
    result = step(state, event, supplied)
    assert result.reply.resolution == "handoff"
    assert result.reply.text == HANDOFF_TEXT
    assert not result.reply.source_ids
    assert not result.reply.recommendations
    assert not result.reply.buttons
    assert result.state.current_node_key == state.current_node_key
    assert result.state.transitions == state.transitions
    assert result.state.outcome == "handoff"


def test_exact_retired_pin_can_continue_but_new_version_cannot_replace_it(
    tree: TreeSnapshot,
) -> None:
    state = step(DiagnosisState(), START, tree).state
    old = replace(tree, retired=True)
    result = step(state, {"type": "button", "option_id": "a"}, old)
    assert result.reply.resolution == "results"
    assert result.state.tree_id == TREE_ID
    resolved = step(result.state, {"type": "feedback", "useful": True}, old)
    assert resolved.state.outcome == "resolved"
    blocked = step(
        state, {"type": "button", "option_id": "a"}, replace(tree, tree_id=UUID(int=99))
    )
    assert blocked.code == "tree_mismatch"
    assert blocked.state.current_node_key == "root"


@pytest.mark.parametrize(
    "kind,risk", list(product(["result", "handoff"], ["observation", "expert_only"]))
)
def test_terminal_start_and_defensive_expert_only(kind: str, risk: str) -> None:
    node = terminal("result" if kind == "result" else "handoff")
    tree = snapshot({"root": node})
    # Inconsistent expert-only result cannot pass MVP-027. Test defense in depth
    # without pretending this corrupted snapshot passed validation/publication.
    tree.content.nodes["root"].risk = (
        "expert_only" if risk == "expert_only" else "observation"
    )
    result = step(DiagnosisState(), START, tree)
    assert result.state.transitions == 0
    if risk == "expert_only" or kind == "handoff":
        assert result.reply.resolution == "handoff"
        assert result.state.outcome == "handoff"
        assert not result.reply.source_ids and not result.reply.recommendations
        assert not result.reply.buttons
    else:
        assert result.reply.resolution == "results"
        assert result.state.outcome == "unresolved"


def test_expert_only_question_is_always_safe(tree: TreeSnapshot) -> None:
    tree.content.nodes["root"].risk = "expert_only"
    tree.content.nodes["root"].text = "SYNTHETIC withheld expert text"
    started = step(DiagnosisState(), START, tree)
    assert started.reply.text == HANDOFF_TEXT
    assert not started.reply.buttons
    assert not started.reply.source_ids
    for event in (
        {"type": "button", "option_id": "a"},
        {"type": "feedback", "useful": True},
    ):
        result = step(started.state, event, tree)
        assert result.state == started.state
        assert result.reply.text == HANDOFF_TEXT


def test_thirtieth_edge_can_reach_result_and_receive_feedback() -> None:
    nodes = {
        f"q{i}": question(("a", f"q{i + 1}"), ("b", f"q{i + 1}"))
        for i in range(MAX_TRANSITIONS)
    }
    nodes[f"q{MAX_TRANSITIONS}"] = terminal("result")
    tree = snapshot(nodes, "q0")
    result = step(DiagnosisState(), START, tree)
    for count in range(1, MAX_TRANSITIONS + 1):
        result = step(result.state, {"type": "button", "option_id": "a"}, tree)
        assert result.state.transitions == count
    assert result.reply.resolution == "results"
    resolved = step(result.state, {"type": "feedback", "useful": True}, tree)
    assert resolved.state.outcome == "resolved"
    assert resolved.state.transitions == 30


@pytest.mark.parametrize("count", [29, 30])
def test_budget_exhaustion_hands_off_without_thirty_first_edge(
    tree: TreeSnapshot, count: int
) -> None:
    state = DiagnosisState(tree_id=TREE_ID, current_node_key="root", transitions=count)
    result = step(state, {"type": "button", "option_id": "continue"}, tree)
    assert result.state.transitions == 30
    assert result.reply.resolution == "handoff"
    assert result.code == "transition_limit"
    assert result.state.current_node_key == ("second" if count == 29 else "root")
    assert not result.reply.buttons and not result.reply.source_ids
    again = step(result.state, {"type": "button", "option_id": "a"}, tree)
    assert again.state == result.state
    assert step(result.state, START, tree).state.transitions == 0


@pytest.mark.parametrize("count", [-1, 31, True])
def test_invalid_internal_counts_fail_explicitly(count: int) -> None:
    with pytest.raises(ValueError, match="INVALID_TRANSITION_COUNT"):
        DiagnosisState(tree_id=TREE_ID, current_node_key="root", transitions=count)


def test_revalidation_and_no_argument_or_cross_call_mutation(
    tree: TreeSnapshot,
) -> None:
    before = tree.content.model_dump()
    state = DiagnosisState()
    event = dict(START)
    first = step(state, event, tree)
    assert first == step(state, event, tree)
    assert event == START
    assert state == DiagnosisState()
    assert tree.content.model_dump() == before
    with pytest.raises(FrozenInstanceError):
        first.state.transitions = 25  # type: ignore[misc]
    # Returned nested DTOs are fresh, never references into caller/tree state.
    first.reply.buttons[0].input.type = "feedback"
    assert step(state, event, tree).reply.buttons[0].input.type == "button"
    malformed = ButtonInput.model_construct(
        type="button", option_id="a", next="result_a"
    )
    malformed.option_id = "other"
    result = step(first.state, malformed, tree)
    assert result.state == first.state
    assert result.code == "invalid_option"
    invalid_feedback = FeedbackInput.model_construct(type="feedback", useful=1)
    rejected = step(first.state, invalid_feedback, tree)
    assert rejected.code == "invalid_input"
    assert rejected.state == first.state


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [], {}])
def test_forged_feedback_at_result_never_resolves(
    tree: TreeSnapshot, value: object
) -> None:
    started = step(DiagnosisState(), START, tree)
    result = step(started.state, {"type": "button", "option_id": "a"}, tree)
    rejected = step(result.state, {"type": "feedback", "useful": value}, tree)
    assert rejected.code == "invalid_input"
    assert rejected.state == result.state
    assert rejected.reply == result.reply


def test_step_never_calls_io_or_tree_loader(
    tree: TreeSnapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("pure transition attempted I/O")

    with monkeypatch.context() as guard:
        for target in (
            "builtins.open",
            "pathlib.Path.open",
            "socket.socket",
            "subprocess.Popen",
            "os.system",
            "sqlalchemy.orm.Session.execute",
            "app.modules.kb.tree_reads.load_tree",
        ):
            guard.setattr(target, forbidden)
        started = step(DiagnosisState(), START, tree)
        text = step(started.state, {"type": "text", "value": "synthetic"}, tree)
        result = step(text.state, {"type": "button", "option_id": "a"}, tree)
        confirmed = step(result.state, {"type": "feedback", "useful": True}, tree)
        assert confirmed.state.outcome == "resolved"
        reset = step(confirmed.state, {"type": "select_mode", "mode": "commerce"}, None)
        assert reset.state.tree_id is None
        assert (
            step(
                started.state, {"type": "text", "value": "synthetic"}, None
            ).reply.resolution
            == "handoff"
        )
