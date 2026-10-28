"""Atomic message exchange. The outer HTTP UoW alone commits or rolls back."""

import hmac
from dataclasses import asdict
from time import monotonic_ns

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db_types import utc_now
from app.modules.chat.access import ChatExpired, require_owner
from app.modules.chat.message_request import minimized_input, request_hash
from app.modules.chat.message_state import (
    ResponseEnvelope,
    StateAfter,
    decode_response,
    restore_state,
)
from app.modules.chat.mode_router import ModeRouter
from app.modules.chat.models import ChatMessage
from app.modules.chat.projection import safe_reply
from app.schemas.chat import ChatMessageCreate, ChatMessageResult


class MessageConflict(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


def public_result(
    message: ChatMessage, envelope: ResponseEnvelope
) -> ChatMessageResult:
    # Single temporary new/replay boundary, shared with GET's safe_reply. The
    # structured logical reply must stay private until the MVP-034 projector.
    return ChatMessageResult(
        message_id=message.id,
        state_version=message.state_version_after,
        reply=safe_reply(envelope.state_after.mode),
    )


def create_message(
    session: Session, body: ChatMessageCreate, token: str | None
) -> ChatMessageResult:
    started = monotonic_ns()
    row = require_owner(session, body.session_id, token, exclusive=True)
    digest = request_hash(body)
    previous = session.scalar(
        select(ChatMessage).where(
            ChatMessage.session_id == row.id, ChatMessage.request_id == body.request_id
        )
    )
    if previous is not None:
        if not hmac.compare_digest(previous.request_hash, digest):
            raise MessageConflict("IDEMPOTENCY_CONFLICT")
        return public_result(previous, decode_response(previous))
    if body.expected_version != row.state_version:
        raise MessageConflict("STATE_CONFLICT")

    state = restore_state(session, row)
    result = ModeRouter(session).route(state, body.input)
    version = row.state_version + 1
    envelope = ResponseEnvelope(
        format_version=1,
        reply=result.reply,
        state_after=StateAfter(**asdict(result.state), state_version=version),
    )
    now = utc_now()
    if row.expires_at <= now:
        raise ChatExpired
    message = ChatMessage(
        session_id=row.id,
        request_id=body.request_id,
        request_hash=digest,
        input=minimized_input(body),
        response=envelope.model_dump(mode="json"),
        state_version_after=version,
        matched_by=(
            "button"
            if body.input.type != "text" and result.code == "ok"
            else "search"
            if body.input.type == "text"
            and state.mode in {"knowledge", "commerce"}
            and result.reply.resolution == "results"
            else "none"
        ),
        latency_ms=min(2_147_483_647, max(0, (monotonic_ns() - started) // 1_000_000)),
        created_at=now,
    )
    row.mode = result.state.mode
    row.tree_id = result.state.tree_id
    row.current_node_key = result.state.current_node_key
    row.outcome = result.state.outcome
    row.state_version = version
    row.last_activity_at = max(row.last_activity_at, now)
    session.add(message)
    session.flush()
    return public_result(message, envelope)
