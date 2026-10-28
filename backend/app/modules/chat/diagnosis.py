"""Pure diagnostic transitions over an already authorized MVP-028 snapshot.

The caller selects published trees for starts and reloads the exact pin for every
subsequent call (including history/replay). Ownership, ACL, expiry, concurrency,
request deduplication and persistence belong to the calling service. A snapshot
is data, never proof of current access. No loader or other I/O is called here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from app.schemas.chat import (
    ButtonInput,
    ChatInput,
    ChatMode,
    ContactSpecialistAction,
    FeedbackInput,
    Resolution,
    SelectModeInput,
    SpecialistPayload,
    StartDiagnosisInput,
    TextInput,
)

if TYPE_CHECKING:
    from app.modules.kb.tree_reads import TreeSnapshot

MAX_TRANSITIONS = 30
HANDOFF_TEXT = "Для продолжения обратитесь к специалисту."
Outcome = Literal["resolved", "handoff", "unresolved"]
StepCode = Literal[
    "ok",
    "invalid_input",
    "invalid_option",
    "feedback_not_allowed",
    "inactive_mode",
    "tree_unavailable",
    "tree_mismatch",
    "transition_limit",
]
_INPUT = TypeAdapter[ChatInput](ChatInput)


@dataclass(frozen=True)
class DiagnosisState:
    """Trusted service state; never construct this from client node/next fields.

    Count only accepted question edges. Starting/restarting and feedback are not
    edges. The integrator must persist/reconstruct this count without resetting
    it on each message. HTTP state_version is a separate service concern.
    """

    mode: ChatMode = "diagnose"
    tree_id: UUID | None = None
    current_node_key: str | None = None
    transitions: int = 0
    outcome: Outcome | None = None

    def __post_init__(self) -> None:
        if (
            type(self.transitions) is not int
            or not 0 <= self.transitions <= MAX_TRANSITIONS
        ):
            raise ValueError("INVALID_TRANSITION_COUNT")
        if self.tree_id is None and (
            self.current_node_key is not None or self.transitions
        ):
            raise ValueError("PROGRESS_REQUIRES_TREE")
        if self.tree_id is not None and (
            self.mode != "diagnose" or self.current_node_key is None
        ):
            raise ValueError("INVALID_DIAGNOSTIC_PIN")


@dataclass(frozen=True)
class DiagnosisButton:
    id: str
    label: str
    input: ButtonInput | FeedbackInput


@dataclass(frozen=True)
class DiagnosisRecommendation:
    item_id: UUID
    qty: int


@dataclass(frozen=True)
class DiagnosisReply:
    """Internal reply, not a public ChatReply or an ACL/catalog projector.

    Source UUIDs and recommendations come only from the supplied snapshot.
    The caller expands them into current SourceRef/catalog cards and validates
    the public reply. Tree option labels retain their approved text verbatim.
    """

    mode: ChatMode
    text: str
    resolution: Resolution
    buttons: tuple[DiagnosisButton, ...] = ()
    source_ids: tuple[UUID, ...] = ()
    recommendations: tuple[DiagnosisRecommendation, ...] = ()
    actions: tuple[ContactSpecialistAction, ...] = ()


@dataclass(frozen=True)
class DiagnosisResult:
    state: DiagnosisState
    reply: DiagnosisReply
    code: StepCode = "ok"


def _specialist_action() -> ContactSpecialistAction:
    return ContactSpecialistAction(
        type="contact_specialist", payload=SpecialistPayload(kind="support")
    )


def _handoff(state: DiagnosisState, code: StepCode = "ok") -> DiagnosisResult:
    return DiagnosisResult(
        replace(state, outcome="handoff"),
        DiagnosisReply(
            mode=state.mode,
            text=HANDOFF_TEXT,
            resolution="handoff",
            actions=(_specialist_action(),),
        ),
        code,
    )


def _current(
    state: DiagnosisState, tree: TreeSnapshot | None, code: StepCode = "ok"
) -> DiagnosisResult:
    if state.mode != "diagnose":
        return DiagnosisResult(
            state,
            DiagnosisReply(state.mode, "Режим выбран.", "no_match"),
            code,
        )
    if tree is None:
        return _handoff(state, "tree_unavailable")
    if state.tree_id != tree.tree_id:
        return _handoff(state, "tree_mismatch")
    node = tree.content.nodes.get(state.current_node_key or "")
    if node is None:
        return _handoff(state, "tree_unavailable")
    # Never expose expert-only text, sources, recommendations or options, even
    # if an integrator accidentally supplies an inconsistent typed snapshot.
    if state.outcome == "handoff" or node.risk == "expert_only":
        return _handoff(state, code)
    if node.type == "handoff":
        result = _handoff(state, code)
        return replace(result, reply=replace(result.reply, text=node.text))
    if node.type == "question" and state.transitions >= MAX_TRANSITIONS:
        return _handoff(state, "transition_limit")
    if node.type == "question":
        buttons = tuple(
            DiagnosisButton(
                option.id,
                option.text,
                ButtonInput(type="button", option_id=option.id),
            )
            for option in node.options
        )
        resolution: Resolution = "question"
    else:
        buttons = (
            ()
            if state.outcome == "resolved"
            else (
                DiagnosisButton(
                    "useful", "Помогло", FeedbackInput(type="feedback", useful=True)
                ),
                DiagnosisButton(
                    "not_useful",
                    "Не помогло",
                    FeedbackInput(type="feedback", useful=False),
                ),
            )
        )
        resolution = "completed" if state.outcome == "resolved" else "results"
        if state.outcome is None:
            state = replace(state, outcome="unresolved")
    return DiagnosisResult(
        state,
        DiagnosisReply(
            mode=state.mode,
            text=node.text,
            resolution=resolution,
            buttons=buttons,
            source_ids=tuple(UUID(source) for source in node.source_refs),
            recommendations=tuple(
                DiagnosisRecommendation(UUID(item.item_id), item.qty)
                for item in node.recommendations
            ),
            actions=(_specialist_action(),) if resolution == "completed" else (),
        ),
        code,
    )


def step(
    state: DiagnosisState,
    input: ChatInput | Mapping[str, object],
    tree: TreeSnapshot | None,
) -> DiagnosisResult:
    """Return a new state/reply without mutating any argument or doing I/O.

    Revalidate even DTO instances: they are mutable and may have been built
    without validation. Extra next/node_id fields and coercible booleans fail
    closed. A rejection never follows an edge or echoes untrusted input.
    None means the external loader could not authorize the requested tree.
    """
    raw = (
        dict(input) if isinstance(input, Mapping) else input.model_dump(warnings=False)
    )
    try:
        event = _INPUT.validate_python(raw)
    except ValidationError:
        return _current(state, tree, "invalid_input")

    if isinstance(event, SelectModeInput):
        if event.mode != state.mode:
            fresh = DiagnosisState(mode=event.mode)
            return DiagnosisResult(
                fresh,
                DiagnosisReply(event.mode, "Режим выбран.", "no_match"),
            )
        return _current(state, tree)
    if state.mode != "diagnose":
        return _current(state, tree, "inactive_mode")
    if isinstance(event, StartDiagnosisInput):
        if tree is None:
            return _handoff(state, "tree_unavailable")
        if (
            tree.retired
            or tree.model_id != event.model_id
            or tree.content.key != event.tree_key
        ):
            return _handoff(state, "tree_mismatch")
        return _current(
            DiagnosisState(tree_id=tree.tree_id, current_node_key=tree.content.start),
            tree,
        )

    current = _current(state, tree)
    # Fail closed before inspecting feedback/buttons when the pin, node, risk
    # or transition budget is unavailable. Terminal handoff is sticky until
    # an explicit restart or mode change.
    if current.reply.resolution in {"handoff", "completed"}:
        code: StepCode = (
            "feedback_not_allowed"
            if isinstance(event, FeedbackInput)
            else "invalid_option"
            if isinstance(event, ButtonInput)
            else current.code
        )
        return replace(current, code=code if current.code == "ok" else current.code)
    if isinstance(event, TextInput):
        return current
    if isinstance(event, FeedbackInput):
        if current.reply.resolution != "results":
            return replace(current, code="feedback_not_allowed")
        if event.useful is True:
            return _current(replace(current.state, outcome="resolved"), tree)
        return _handoff(current.state)
    if isinstance(event, ButtonInput):
        if current.reply.resolution != "question":
            return replace(current, code="invalid_option")
        assert tree is not None
        node = tree.content.nodes[current.state.current_node_key or ""]
        option = next((o for o in node.options if o.id == event.option_id), None)
        if option is None:
            return replace(current, code="invalid_option")
        return _current(
            replace(
                current.state,
                current_node_key=option.next,
                transitions=current.state.transitions + 1,
            ),
            tree,
        )
    return replace(current, code="invalid_input")
