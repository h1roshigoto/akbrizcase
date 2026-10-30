"""Technical commands, never copies of customer data or verification material."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_types import Base


class DeletionEntry(Base):
    __tablename__ = "deletion_journal"
    __table_args__ = (UniqueConstraint("operation", "object_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    format: Mapped[int] = mapped_column(server_default=text("1"))
    operation: Mapped[str] = mapped_column(String(32))
    object_id: Mapped[UUID]
    consent_id: Mapped[UUID | None]
    decision_id: Mapped[UUID | None]
    policy_version: Mapped[str | None] = mapped_column(
        ForeignKey("app.consent_publications.consent_version", ondelete="RESTRICT")
    )
    journal_policy_version: Mapped[str | None] = mapped_column(
        ForeignKey("app.consent_publications.consent_version", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime]
    retain_until: Mapped[datetime]
    applied_at: Mapped[datetime | None]


class CleanupCursor(Base):
    __tablename__ = "cleanup_cursors"

    category: Mapped[str] = mapped_column(String(32), primary_key=True)
    after_id: Mapped[UUID | None]
    next_run_at: Mapped[datetime]
