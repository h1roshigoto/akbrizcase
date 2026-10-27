"""Diagnostic editions and normalized, FK-protected dependencies (§5.3)."""

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_types import Base


class DiagnosticTree(Base):
    __tablename__ = "diag_trees"
    __table_args__ = (
        UniqueConstraint("key", "version"),
        CheckConstraint("version > 0", name="version"),
        CheckConstraint("key ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$'", name="key"),
        CheckConstraint(
            "title = btrim(title) AND title <> '' AND title !~ '[[:cntrl:]]'",
            name="title",
        ),
        CheckConstraint(
            "status IN ('draft','published','retired','revoked')", name="status"
        ),
        CheckConstraint("jsonb_typeof(graph) = 'object'", name="graph"),
        CheckConstraint("yaml_checksum ~ '^[0-9a-f]{64}$'", name="yaml_checksum"),
        CheckConstraint(
            "git_commit ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'", name="git_commit"
        ),
        CheckConstraint(
            "(expert_name IS NULL AND review_reference IS NULL "
            "AND approved_at IS NULL AND published_by IS NULL "
            "AND published_at IS NULL AND status IN ('draft','revoked')) OR "
            "(status <> 'draft' AND expert_name IS NOT NULL "
            "AND length(expert_name) BETWEEN 3 AND 200 "
            "AND expert_name = btrim(expert_name) "
            "AND expert_name !~ '[[:cntrl:]]' "
            "AND review_reference IS NOT NULL "
            "AND review_reference ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{2,199}$' "
            "AND approved_at IS NOT NULL AND isfinite(approved_at) "
            "AND published_by IS NOT NULL AND published_at IS NOT NULL "
            "AND isfinite(published_at) AND approved_at <= published_at)",
            name="approval",
        ),
        Index(
            "ix_diag_trees_published",
            "key",
            unique=True,
            postgresql_where=sql_text("status = 'published'"),
        ),
        Index("ix_diag_trees_model_status_key", "model_id", "status", "key"),
        Index("ix_diag_trees_status_key", "status", "key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(100))
    version: Mapped[int]
    model_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.equipment_models.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(String(200))
    graph: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    yaml_checksum: Mapped[str] = mapped_column(String(64))
    git_commit: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default="draft")
    expert_name: Mapped[str | None] = mapped_column(String(200))
    review_reference: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None]
    published_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("app.staff_users.id", ondelete="RESTRICT")
    )
    published_at: Mapped[datetime | None]


class DiagnosticTreeSource(Base):
    __tablename__ = "diag_tree_sources"
    __table_args__ = (
        CheckConstraint(
            "node_key ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$'", name="node_key"
        ),
        Index("ix_diag_tree_sources_section_id", "section_id"),
    )

    tree_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.diag_trees.id", ondelete="RESTRICT"), primary_key=True
    )
    node_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    section_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.document_sections.id", ondelete="RESTRICT"), primary_key=True
    )


class DiagnosticTreeItem(Base):
    __tablename__ = "diag_tree_items"
    __table_args__ = (
        CheckConstraint(
            "node_key ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$'", name="node_key"
        ),
        CheckConstraint("qty BETWEEN 1 AND 999", name="qty"),
        Index("ix_diag_tree_items_item_id", "item_id"),
    )

    tree_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.diag_trees.id", ondelete="RESTRICT"), primary_key=True
    )
    node_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    item_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.catalog_items.id", ondelete="RESTRICT"), primary_key=True
    )
    qty: Mapped[int]
