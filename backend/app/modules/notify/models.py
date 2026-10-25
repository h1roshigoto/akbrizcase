"""Payload-free notification storage. Sending/retries belong to MVP-017."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_types import Base


class EmailOutbox(Base):
    __tablename__ = "email_outbox"
    __table_args__ = (
        UniqueConstraint("order_id", "event_kind"),
        CheckConstraint("event_kind = 'manager_notification'", name="event_kind"),
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed')", name="status"
        ),
        CheckConstraint("attempts >= 0", name="attempts"),
        CheckConstraint(
            "(status = 'sending' AND lease_until IS NOT NULL "
            "AND lease_token IS NOT NULL) OR (status <> 'sending' "
            "AND lease_until IS NULL AND lease_token IS NULL)",
            name="lease",
        ),
        CheckConstraint(
            "(status = 'sent' AND sent_at IS NOT NULL) "
            "OR (status <> 'sent' AND sent_at IS NULL)",
            name="sent_at",
        ),
        CheckConstraint(
            "last_error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'", name="error_code"
        ),
        Index(
            "ix_email_outbox_pending_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_email_outbox_sending_lease",
            "lease_until",
            postgresql_where=text("status = 'sending'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.orders.id", ondelete="CASCADE")
    )
    event_kind: Mapped[str] = mapped_column(
        String(32), server_default="manager_notification"
    )
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    lease_until: Mapped[datetime | None]
    lease_token: Mapped[UUID | None]
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    sent_at: Mapped[datetime | None]
