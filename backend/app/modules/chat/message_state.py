"""Private response format v1; never return this envelope through HTTP.

MVP-034 can consume its validated logical reply/state, but must project them
against current authorization and dependencies for new replies/history/replay.
"""

from typing import Annotated, cast

from pydantic import Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.chat.diagnosis import MAX_TRANSITIONS, DiagnosisState, Outcome
from app.modules.chat.models import ChatMessage, ChatSession
from app.schemas.chat import ChatMode, ChatReply
from app.schemas.common import DTO, Code, PositiveInt, UUIDValue


class StoredStateUnavailable(Exception):
    """Missing/corrupt progress must never silently become zero transitions."""


class StateAfter(DTO):
    mode: ChatMode
    tree_id: UUIDValue | None
    current_node_key: Code | None
    outcome: Outcome | None
    transitions: Annotated[int, Field(ge=0, le=MAX_TRANSITIONS)]
    state_version: PositiveInt

    def diagnosis(self) -> DiagnosisState:
        return DiagnosisState(
            mode=self.mode,
            tree_id=self.tree_id,
            current_node_key=self.current_node_key,
            outcome=self.outcome,
            transitions=self.transitions,
        )

    @model_validator(mode="after")
    def consistent_progress(self) -> "StateAfter":
        self.diagnosis()
        if self.transitions > self.state_version:
            raise ValueError("INVALID_STORED_PROGRESS")
        return self


class ResponseEnvelope(DTO):
    format_version: Annotated[int, Field(ge=1, le=1)]
    reply: ChatReply
    state_after: StateAfter

    @model_validator(mode="after")
    def consistent_mode(self) -> "ResponseEnvelope":
        if self.reply.mode != self.state_after.mode:
            raise ValueError("INVALID_STORED_MODE")
        return self


def decode_response(message: ChatMessage) -> ResponseEnvelope:
    try:
        envelope = ResponseEnvelope.model_validate(message.response)
    except ValidationError:
        raise StoredStateUnavailable from None
    if envelope.state_after.state_version != message.state_version_after:
        raise StoredStateUnavailable
    return envelope


def restore_state(session: Session, row: ChatSession) -> DiagnosisState:
    # A timestamp/UUID ordering is not the state sequence. Detect both a missing
    # last exchange and an ambiguous duplicate version (legacy DB has no UNIQUE
    # state_version_after constraint). The exclusive session lock serializes us.
    latest = session.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == row.id)
        .order_by(ChatMessage.state_version_after.desc())
        .limit(2)
    ).all()
    if row.state_version == 0:
        if latest or any((row.tree_id, row.current_node_key, row.outcome)):
            raise StoredStateUnavailable
        return DiagnosisState(mode=cast(ChatMode, row.mode))
    if (
        not latest
        or latest[0].state_version_after != row.state_version
        or (len(latest) > 1 and latest[1].state_version_after == row.state_version)
    ):
        raise StoredStateUnavailable
    state = decode_response(latest[0]).state_after
    if (state.mode, state.tree_id, state.current_node_key, state.outcome) != (
        row.mode,
        row.tree_id,
        row.current_node_key,
        row.outcome,
    ):
        raise StoredStateUnavailable
    return state.diagnosis()
