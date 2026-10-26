"""Document → immutable edition → stable source sections (architecture §5.2)."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_types import Base, utc_now


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('manual', 'regulation', 'article', 'other')", name="kind"
        ),
        CheckConstraint("visibility IN ('public', 'internal')", name="visibility"),
        CheckConstraint("status IN ('active', 'revoked')", name="status"),
        CheckConstraint("btrim(code) <> ''", name="code"),
        CheckConstraint("btrim(title) <> ''", name="title"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(100), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))
    visibility: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=utc_now
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version"),
        CheckConstraint("version > 0", name="version"),
        CheckConstraint(
            "status IN ('draft', 'published', 'retired', 'revoked')", name="status"
        ),
        CheckConstraint("size_bytes BETWEEN 1 AND 26214400", name="size_bytes"),
        CheckConstraint("checksum_sha256 ~ '^[0-9a-f]{64}$'", name="checksum_sha256"),
        CheckConstraint(
            "file_key ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$'", name="file_key"
        ),
        CheckConstraint("btrim(mime) <> ''", name="mime"),
        Index(
            "ix_document_versions_published",
            "document_id",
            unique=True,
            postgresql_where=sql_text("status = 'published'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.documents.id", ondelete="RESTRICT")
    )
    version: Mapped[int]
    file_key: Mapped[str] = mapped_column(String(200))
    mime: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int]
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default="draft")
    reviewed_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("app.staff_users.id", ondelete="RESTRICT")
    )
    approved_at: Mapped[datetime | None]
    published_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=utc_now
    )


class DocumentSection(Base):
    __tablename__ = "document_sections"
    __table_args__ = (
        UniqueConstraint("document_version_id", "section_key"),
        CheckConstraint("btrim(section_key) <> ''", name="section_key"),
        CheckConstraint(
            "(page_from IS NULL AND page_to IS NULL) OR "
            "(page_from IS NOT NULL AND page_to IS NOT NULL "
            "AND page_from >= 1 AND page_to >= page_from)",
            name="pages",
        ),
        Index(
            "ix_document_sections_search_vector",
            "search_vector",
            postgresql_using="gin",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.document_versions.id", ondelete="RESTRICT"), index=True
    )
    section_key: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(200))
    page_from: Mapped[int | None]
    page_to: Mapped[int | None]
    text: Mapped[str] = mapped_column(Text)
    assistant_allowed: Mapped[bool] = mapped_column(server_default=sql_text("false"))
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('russian'::regconfig, title || ' ' || text)", persisted=True
        ),
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=utc_now
    )
