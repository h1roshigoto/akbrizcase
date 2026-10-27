"""Equipment and the shared product/service catalog (architecture §5.1)."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_types import Base, utc_now
from app.modules.kb import models as kb_models  # noqa: F401 -- register manual FK target


class EquipmentModel(Base):
    __tablename__ = "equipment_models"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'published', 'archived')", name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default="draft")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=utc_now
    )
    manual_document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("app.documents.id", ondelete="RESTRICT")
    )


class CatalogItem(Base):
    __tablename__ = "catalog_items"
    __table_args__ = (
        CheckConstraint("kind IN ('product', 'service')", name="kind"),
        CheckConstraint("status IN ('draft', 'published', 'archived')", name="status"),
        CheckConstraint("price_mode IN ('fixed', 'on_request')", name="price_mode"),
        CheckConstraint(
            "(price_mode = 'fixed' AND price IS NOT NULL AND price >= 0 "
            "AND price <> 'NaN'::numeric) "
            "OR (price_mode = 'on_request' AND price IS NULL)",
            name="price",
        ),
        CheckConstraint("kind <> 'service' OR NOT requires_shipping", name="shipping"),
        CheckConstraint("revision > 0", name="revision"),
        CheckConstraint("currency = 'RUB'", name="currency"),
        CheckConstraint(
            "jsonb_typeof(attributes) = 'object'", name="attributes_object"
        ),
        Index(
            "ix_catalog_items_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "public.gin_trgm_ops"},
        ),
        Index(
            "ix_catalog_items_sku_trgm",
            "sku",
            postgresql_using="gin",
            postgresql_ops={"sku": "public.gin_trgm_ops"},
        ),
        Index(
            "ix_catalog_items_published_kind",
            "kind",
            postgresql_where=text("status = 'published'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    sku: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    # Display data only: never evidence of equipment compatibility.
    attributes: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    unit: Mapped[str] = mapped_column(String(200))
    price_mode: Mapped[str] = mapped_column(String(16))
    price: Mapped[Decimal | None]
    currency: Mapped[str] = mapped_column(String(3), server_default="RUB")
    tax_note: Mapped[str] = mapped_column(String(200))
    requires_shipping: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    image_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default="draft")
    revision: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=utc_now
    )


class CatalogItemModel(Base):
    """A candidate, with review and disclosure permission kept separate."""

    __tablename__ = "catalog_item_models"
    __table_args__ = (
        CheckConstraint(
            "expert_name IS NULL OR (length(expert_name) BETWEEN 3 AND 200 "
            "AND expert_name = btrim(expert_name) "
            "AND expert_name !~ '[[:cntrl:]]')",
            name="expert_name",
        ),
        CheckConstraint(
            "review_reference IS NULL OR "
            "review_reference ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{2,199}$'",
            name="review_reference",
        ),
        CheckConstraint(
            "approved_at IS NULL OR isfinite(approved_at)", name="approved_at"
        ),
        CheckConstraint(
            "(NOT public_allowed AND published_by IS NULL AND published_at IS NULL) "
            "OR (public_allowed AND expert_name IS NOT NULL "
            "AND review_reference IS NOT NULL AND approved_at IS NOT NULL "
            "AND published_by IS NOT NULL AND published_at IS NOT NULL "
            "AND isfinite(published_at) AND approved_at <= published_at)",
            name="publication",
        ),
        Index("ix_catalog_item_models_model_item", "model_id", "item_id"),
        Index("ix_catalog_item_models_evidence_section_id", "evidence_section_id"),
    )

    item_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.catalog_items.id", ondelete="RESTRICT"), primary_key=True
    )
    model_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.equipment_models.id", ondelete="RESTRICT"), primary_key=True
    )
    evidence_section_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.document_sections.id", ondelete="RESTRICT")
    )
    expert_name: Mapped[str | None] = mapped_column(String(200))
    review_reference: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None]
    public_allowed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    published_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("app.staff_users.id", ondelete="RESTRICT")
    )
    published_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=utc_now
    )
