"""Staff identities, server sessions and minimal append-only audit records."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_types import Base, utc_now


class StaffUser(Base):
    __tablename__ = "staff_users"
    __table_args__ = (
        CheckConstraint("role IN ('manager', 'admin')", name="role"),
        CheckConstraint("login ~ '^[A-Za-z0-9_.-]{3,64}$'", name="login_format"),
        CheckConstraint("password_hash LIKE '$argon2id$%'", name="password_algorithm"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    login: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )


class StaffSession(Base):
    __tablename__ = "staff_sessions"
    __table_args__ = (
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_format"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.staff_users.id", ondelete="RESTRICT"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(index=True)
    revoked_at: Mapped[datetime | None]


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_entity", "entity_type", "entity_id", "at"),
        Index(
            "uq_audit_events_bootstrap_success",
            "action",
            unique=True,
            postgresql_where=text("action = 'staff.bootstrap' AND result = 'success'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("app.staff_users.id", ondelete="RESTRICT"), index=True
    )
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[UUID | None]
    at: Mapped[datetime] = mapped_column(default=utc_now, index=True)
    result: Mapped[str] = mapped_column(String(32))
    # SQLAlchemy reserves the Python attribute name 'metadata'.
    details: Mapped[dict[str, str | bool]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
