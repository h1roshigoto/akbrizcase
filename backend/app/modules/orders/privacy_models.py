"""Immutable publication packet binds consent, policy and retention approval."""

from datetime import datetime

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_types import Base


class ConsentPublication(Base):
    __tablename__ = "consent_publications"

    consent_version: Mapped[str] = mapped_column(
        ForeignKey("app.consent_texts.version", ondelete="RESTRICT"), primary_key=True
    )
    bundle_checksum: Mapped[str] = mapped_column(Text)
    # Public documents/settings, never signatures or private evidence.
    bundle: Mapped[dict[str, object]] = mapped_column(JSONB)
    published_at: Mapped[datetime]
