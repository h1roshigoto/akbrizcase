"""Allowlisted staff projections; never serialize sensitive ORM entities."""

from typing import Literal

from app.schemas.common import (
    DTO,
    CatalogKind,
    Currency,
    Money,
    OrderKind,
    OrderStatus,
    Page,
    Pagination,
    PositiveInt,
    PriceMode,
    UTCDateTime,
    UUIDValue,
)


class OrderReadQuery(Pagination):
    status: OrderStatus | None = None


class OrderSummary(DTO):
    id: UUIDValue
    number: PositiveInt
    created_at: UTCDateTime
    kind: OrderKind
    status: OrderStatus
    known_subtotal: Money
    items_total: Money | None
    delivery_status: Literal["not_required", "pending_quote"]
    delivery_cost: Money | None
    total_amount: Money | None
    currency: Currency


class ItemSnapshot(DTO):
    id: UUIDValue
    item_id: UUIDValue
    qty: PositiveInt
    sku_snapshot: str
    name_snapshot: str
    kind_snapshot: CatalogKind
    unit_snapshot: str
    price_mode_snapshot: PriceMode
    unit_price_snapshot: Money | None
    currency_snapshot: Currency
    tax_note_snapshot: str
    requires_shipping_snapshot: bool
    catalog_revision_snapshot: PositiveInt


class ConsentEvidence(DTO):
    id: UUIDValue
    text_version: str
    granted_at: UTCDateTime
    channel: Literal["request_form"]
    revoked_at: UTCDateTime | None
    purpose: str
    checksum: str


class StatusEvent(DTO):
    id: UUIDValue
    from_status: OrderStatus | None
    to_status: OrderStatus
    changed_by: UUIDValue | None
    reason: str | None
    at: UTCDateTime


class OrderDetail(OrderSummary):
    source: Literal["site", "chat"]
    version: PositiveInt
    updated_at: UTCDateTime
    closed_at: UTCDateTime | None
    pii_erased_at: UTCDateTime | None
    contact_name: str | None
    email: str | None
    phone: str | None
    organization_name: str | None
    city: str | None
    comment: str | None
    consent: ConsentEvidence
    items: list[ItemSnapshot]
    history: list[StatusEvent]
    notification_status: Literal["pending", "sending", "sent", "failed"] | None


OrderPage = Page[OrderSummary]
