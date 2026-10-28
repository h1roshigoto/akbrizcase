"""Session lifecycle only. No message writes or diagnostic transitions."""

import secrets
from datetime import timedelta
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.core.db_types import utc_now
from app.modules.chat.access import ChatNotFound, owner_token_hash, require_owner
from app.modules.chat.models import ChatMessage, ChatSession
from app.modules.chat.projection import safe_reply
from app.schemas.chat import (
    ChatExchangeRead,
    ChatMode,
    ChatSessionCreate,
    ChatSessionCreated,
    ChatSessionRead,
)


def create_session(session: Session, body: ChatSessionCreate) -> ChatSessionCreated:
    token = secrets.token_hex(32)
    created = utc_now()
    row = ChatSession(
        owner_token_hash=owner_token_hash(token),
        mode=body.mode,
        created_at=created,
        last_activity_at=created,
        expires_at=created + timedelta(hours=24),
    )
    session.add(row)
    session.flush()
    return ChatSessionCreated(
        session_id=row.id,
        session_token=token,
        state_version=0,
        expires_at=row.expires_at,
        reply=safe_reply(body.mode),
    )


def read_session(
    session: Session, identifier: UUID, token: str | None, before: UUID | None
) -> ChatSessionRead:
    row = require_owner(session, identifier, token)
    mode = cast(ChatMode, row.mode)
    # Deliberately select metadata only: raw JSON never crosses the read boundary.
    query = select(
        ChatMessage.id, ChatMessage.created_at, ChatMessage.state_version_after
    ).where(ChatMessage.session_id == row.id)
    if before is not None:
        cursor = session.execute(
            select(ChatMessage.created_at, ChatMessage.id).where(
                ChatMessage.session_id == row.id, ChatMessage.id == before
            )
        ).one_or_none()
        if cursor is None:
            raise ChatNotFound
        query = query.where(
            tuple_(ChatMessage.created_at, ChatMessage.id)
            < tuple_(cursor.created_at, cursor.id)
        )
    records = session.execute(
        query.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(51)
    ).all()
    page = records[:50]
    return ChatSessionRead(
        session_id=row.id,
        mode=mode,
        state_version=row.state_version,
        outcome=cast(Literal["resolved", "handoff", "unresolved"] | None, row.outcome),
        created_at=row.created_at,
        last_activity_at=row.last_activity_at,
        expires_at=row.expires_at,
        reply=safe_reply(mode),
        messages=[
            ChatExchangeRead(
                message_id=record.id,
                state_version=record.state_version_after,
                created_at=record.created_at,
                reply=safe_reply(mode),
            )
            for record in reversed(page)
        ],
        next_before=page[-1].id if len(records) > 50 else None,
    )
