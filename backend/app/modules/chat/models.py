"""One session and one row per atomic request/response exchange (§4.4, §5.5)."""

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_types import Base


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        CheckConstraint("owner_token_hash ~ '^[0-9a-f]{64}$'", name="owner_token_hash"),
        CheckConstraint("mode IN ('diagnose','knowledge','commerce')", name="mode"),
        CheckConstraint("state_version >= 0", name="state_version"),
        CheckConstraint(
            "outcome IN ('resolved','handoff','unresolved')", name="outcome"
        ),
        CheckConstraint(
            "current_node_key IS NULL OR (tree_id IS NOT NULL AND "
            "current_node_key ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')",
            name="current_node_key",
        ),
        CheckConstraint("tree_id IS NULL OR mode = 'diagnose'", name="tree_mode"),
        CheckConstraint(
            "isfinite(created_at) AND isfinite(last_activity_at) "
            "AND isfinite(expires_at) AND last_activity_at >= created_at "
            "AND last_activity_at <= expires_at "
            "AND expires_at = created_at + interval '24 hours'",
            name="times",
        ),
        Index("ix_chat_sessions_expires_at", "expires_at"),
        Index("ix_chat_sessions_tree_id", "tree_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Only a digest is stored. Session UUID is a locator, never ownership proof.
    owner_token_hash: Mapped[str] = mapped_column(String(64))
    retention_version: Mapped[str | None] = mapped_column(
        ForeignKey("app.consent_publications.consent_version", ondelete="RESTRICT")
    )
    mode: Mapped[str] = mapped_column(String(16))
    tree_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("app.diag_trees.id", ondelete="RESTRICT")
    )
    current_node_key: Mapped[str | None] = mapped_column(String(100))
    state_version: Mapped[int] = mapped_column(server_default=text("0"))
    outcome: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_activity_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(
        server_default=text("now() + interval '24 hours'")
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "request_id"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="request_hash"),
        CheckConstraint("jsonb_typeof(input) = 'object'", name="input"),
        CheckConstraint("jsonb_typeof(response) = 'object'", name="response"),
        CheckConstraint("state_version_after > 0", name="state_version_after"),
        CheckConstraint(
            "matched_by IN ('button','keyword','search','none')", name="matched_by"
        ),
        CheckConstraint("latency_ms >= 0", name="latency_ms"),
        CheckConstraint("isfinite(created_at)", name="created_at"),
        Index("ix_chat_messages_session_id_created_at", "session_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.chat_sessions.id", ondelete="CASCADE")
    )
    request_id: Mapped[UUID]
    request_hash: Mapped[str] = mapped_column(String(64))
    input: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    response: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    state_version_after: Mapped[int]
    matched_by: Mapped[str] = mapped_column(String(16))
    latency_ms: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
