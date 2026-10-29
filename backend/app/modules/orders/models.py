"""Order storage only; MVP-009 owns cross-row creation and aggregation rules."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_types import Base, utc_now
from app.modules.chat import models as chat_models  # noqa: F401 -- register FK target
from app.modules.orders import privacy_models  # noqa: F401 -- register publication

ORDER_STATUSES = "('new', 'in_progress', 'closed', 'cancelled')"
order_number_sequence = Sequence(
    "order_number_seq", schema="app", metadata=Base.metadata
)


class ConsentText(Base):
    __tablename__ = "consent_texts"

    # The migration protects version identity/content from UPDATE and DELETE.
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    purpose: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(Text)
    active_from: Mapped[datetime]
    retired_at: Mapped[datetime | None]


class Consent(Base):
    __tablename__ = "consents"
    __table_args__ = (CheckConstraint("channel = 'request_form'", name="channel"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    text_version: Mapped[str] = mapped_column(
        ForeignKey("app.consent_texts.version", ondelete="RESTRICT"), index=True
    )
    granted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    channel: Mapped[str] = mapped_column(String(16), server_default="request_form")
    revoked_at: Mapped[datetime | None]


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("kind IN ('commercial', 'support')", name="kind"),
        CheckConstraint("source IN ('site', 'chat')", name="source"),
        CheckConstraint(f"status IN {ORDER_STATUSES}", name="status"),
        CheckConstraint("version > 0", name="version"),
        CheckConstraint(
            "pii_erased_at IS NOT NULL OR (contact_name IS NOT NULL "
            "AND length(trim(contact_name)) > 0 "
            "AND (coalesce(length(trim(email)), 0) > 0 "
            "OR coalesce(length(trim(phone)), 0) > 0) "
            "AND request_hash IS NOT NULL AND length(request_hash) > 0 "
            "AND receipt_token_hash IS NOT NULL AND length(receipt_token_hash) > 0)",
            name="contact_evidence",
        ),
        CheckConstraint(
            "kind <> 'support' OR pii_erased_at IS NOT NULL OR "
            "(comment IS NOT NULL AND length(trim(comment)) BETWEEN 1 AND 2000)",
            name="support_comment",
        ),
        CheckConstraint("comment IS NULL OR length(comment) <= 2000", name="comment"),
        CheckConstraint("currency = 'RUB'", name="currency"),
        CheckConstraint(
            "known_subtotal >= 0 AND known_subtotal < 'Infinity'::numeric "
            "AND (items_total IS NULL OR (items_total >= 0 "
            "AND items_total < 'Infinity'::numeric)) "
            "AND (delivery_cost IS NULL OR (delivery_cost >= 0 "
            "AND delivery_cost < 'Infinity'::numeric)) "
            "AND (total_amount IS NULL OR (total_amount >= 0 "
            "AND total_amount < 'Infinity'::numeric))",
            name="amounts",
        ),
        CheckConstraint(
            "(delivery_status = 'not_required' AND delivery_cost IS NOT NULL "
            "AND delivery_cost = 0) OR "
            "(delivery_status = 'pending_quote' AND delivery_cost IS NULL)",
            name="delivery",
        ),
        CheckConstraint(
            "(kind = 'support' AND known_subtotal = 0 AND items_total IS NULL "
            "AND total_amount IS NULL AND delivery_status = 'not_required') OR "
            "(kind = 'commercial' "
            "AND (items_total IS NULL OR items_total = known_subtotal) "
            "AND CASE WHEN items_total IS NOT NULL AND delivery_cost IS NOT NULL "
            "THEN total_amount IS NOT NULL "
            "AND total_amount = items_total + delivery_cost "
            "ELSE total_amount IS NULL END)",
            name="totals",
        ),
        Index("ix_orders_status_created_at", "status", text("created_at DESC")),
        Index("ix_orders_chat_session_id", "chat_session_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    number: Mapped[int] = mapped_column(
        BigInteger,
        order_number_sequence,
        server_default=order_number_sequence.next_value(),
        unique=True,
    )
    kind: Mapped[str] = mapped_column(String(16))
    organization_name: Mapped[str | None] = mapped_column(String(200))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(32))
    city: Mapped[str | None] = mapped_column(String(200))
    comment: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), server_default="site")
    chat_session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("app.chat_sessions.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), server_default="new")
    version: Mapped[int] = mapped_column(server_default=text("1"))
    consent_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.consents.id", ondelete="RESTRICT"), unique=True
    )
    idempotency_key: Mapped[UUID] = mapped_column(unique=True)
    request_hash: Mapped[str | None] = mapped_column(Text)
    receipt_token_hash: Mapped[str | None] = mapped_column(Text)
    known_subtotal: Mapped[Decimal]
    items_total: Mapped[Decimal | None]
    delivery_status: Mapped[str] = mapped_column(String(16))
    delivery_cost: Mapped[Decimal | None]
    total_amount: Mapped[Decimal | None]
    currency: Mapped[str] = mapped_column(String(3), server_default="RUB")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=utc_now
    )
    closed_at: Mapped[datetime | None]
    pii_erased_at: Mapped[datetime | None]


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        UniqueConstraint("order_id", "item_id"),
        CheckConstraint("qty BETWEEN 1 AND 999", name="qty"),
        CheckConstraint("catalog_revision_snapshot > 0", name="revision"),
        CheckConstraint("kind_snapshot IN ('product', 'service')", name="kind"),
        CheckConstraint(
            "price_mode_snapshot IN ('fixed', 'on_request')", name="price_mode"
        ),
        CheckConstraint(
            "(price_mode_snapshot = 'fixed' AND unit_price_snapshot IS NOT NULL "
            "AND unit_price_snapshot >= 0 "
            "AND unit_price_snapshot < 'Infinity'::numeric) OR "
            "(price_mode_snapshot = 'on_request' AND unit_price_snapshot IS NULL)",
            name="price",
        ),
        CheckConstraint(
            "kind_snapshot <> 'service' OR NOT requires_shipping_snapshot",
            name="shipping",
        ),
        CheckConstraint("currency_snapshot = 'RUB'", name="currency"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.orders.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.catalog_items.id", ondelete="RESTRICT"), index=True
    )
    qty: Mapped[int]
    sku_snapshot: Mapped[str] = mapped_column(String(200))
    name_snapshot: Mapped[str] = mapped_column(String(200))
    kind_snapshot: Mapped[str] = mapped_column(String(16))
    unit_snapshot: Mapped[str] = mapped_column(String(200))
    price_mode_snapshot: Mapped[str] = mapped_column(String(16))
    unit_price_snapshot: Mapped[Decimal | None]
    currency_snapshot: Mapped[str] = mapped_column(String(3))
    tax_note_snapshot: Mapped[str] = mapped_column(String(200))
    requires_shipping_snapshot: Mapped[bool]
    catalog_revision_snapshot: Mapped[int]


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"
    __table_args__ = (
        CheckConstraint(f"from_status IN {ORDER_STATUSES}", name="from_status"),
        CheckConstraint(f"to_status IN {ORDER_STATUSES}", name="to_status"),
        Index("ix_order_status_history_order_id_at", "order_id", "at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.orders.id", ondelete="CASCADE")
    )
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    changed_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("app.staff_users.id", ondelete="RESTRICT"), index=True
    )
    reason: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(server_default=func.now())
