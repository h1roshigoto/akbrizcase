"""Equipment and the shared product/service catalog (architecture §5.1)."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import Boolean, CheckConstraint, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_types import Base, utc_now


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
    # manual_document_id is added with the real documents table in MVP-019.


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
