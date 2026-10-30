"""Atomic message exchange. The outer HTTP UoW alone commits or rolls back."""

import hmac
from dataclasses import asdict
from time import monotonic_ns
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db_types import utc_now
from app.modules.chat.access import ChatExpired, ChatNotFound, require_owner
from app.modules.chat.message_request import minimized_input, request_hash
from app.modules.chat.message_state import (
    ResponseEnvelope,
    StateAfter,
    restore_state,
)
from app.modules.chat.mode_router import ModeRouter
from app.modules.chat.models import ChatMessage
from app.modules.chat.projection import ChatReplyProjector
from app.modules.orders.privacy_service import lock_privacy
from app.modules.orders.service import collection_gate
from app.schemas.chat import ChatMessageCreate, ChatMessageResult, ChatMode


class MessageConflict(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


def read_message_result(
    session: Session, session_id: UUID, message_id: UUID, token: str | None
) -> ChatMessageResult:
    row = require_owner(session, session_id, token)
    message = session.scalar(
        select(ChatMessage)
        .where(ChatMessage.session_id == row.id, ChatMessage.id == message_id)
        .execution_options(populate_existing=True)
    )
    if message is None:
        raise ChatNotFound
    return ChatMessageResult(
        message_id=message.id,
        state_version=message.state_version_after,
        reply=ChatReplyProjector(session).message(
            message, cast(ChatMode, row.mode), current=row
        ),
    )


def create_message(
    session: Session, body: ChatMessageCreate, token: str | None
) -> UUID:
    started = monotonic_ns()
    # Match orders' privacy -> chat lock order. Retirement may serialize first,
    # but neither configuration nor active consent is needed to authenticate replay.
    lock_privacy(session, shared=True)
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
        return previous.id
    # Owner/expiry and immutable request identity take precedence. This gate is
    # reached only for a new exchange, before routing, state changes or INSERT.
    collection_gate(session)
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
    return message.id
