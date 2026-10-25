"""Atomic creation using the caller's Session. No commit, replay or delivery."""

import hashlib
import os
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.core.staff_sessions import token_hash
from app.modules.catalog.models import CatalogItem
from app.modules.notify.models import EmailOutbox
from app.modules.orders.models import (
    Consent,
    ConsentText,
    Order,
    OrderItem,
    OrderStatusHistory,
)
from app.modules.orders.security import OrderSecurity
from app.modules.orders.seed import DEMO_VERSION
from app.schemas.common import OrderHeaders
from app.schemas.consent import ConsentText as ConsentTextDTO
from app.schemas.orders import OrderCreate, OrderReceipt

ZERO = Decimal("0.00")
MAX_AMOUNT = Decimal("9999999999.99")  # Existing numeric(12,2) storage.


class OrderRejected(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def effective_consent(session: Session, *, lock: bool = False) -> ConsentTextDTO:
    query = (
        select(ConsentText)
        .where(
            ConsentText.active_from <= func.clock_timestamp(),
            or_(
                ConsentText.retired_at.is_(None),
                ConsentText.retired_at > func.clock_timestamp(),
            ),
        )
        .order_by(ConsentText.version)
        .execution_options(populate_existing=True)
    )
    if lock:
        # SHARE blocks retirement through commit; concurrent creations may read.
        # Re-evaluate after waiting on a publisher, using current wall-clock time.
        list(session.scalars(query.with_for_update(read=True)))
        query = query.with_for_update(read=True)
    rows = list(session.scalars(query))
    if len(rows) != 1:
        raise OrderRejected(503, "SERVICE_UNAVAILABLE", "Consent unavailable")
    row = rows[0]
    if row.version == DEMO_VERSION:
        mode = os.environ.get("APP_ENV")
        marker = session.scalar(
            text(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                "WHERE datname = current_database()"
            )
        )
        if (
            mode not in {"demo", "test"}
            or marker != f"akbrizcase:synthetic-seed:{mode}"
        ):
            raise OrderRejected(503, "SERVICE_UNAVAILABLE", "Consent unavailable")
    try:
        result = ConsentTextDTO(
            version=row.version,
            purpose=row.purpose,
            text=row.text,
            checksum=row.checksum,
        )
        if hashlib.sha256(row.text.encode("utf-8")).hexdigest() != row.checksum:
            raise ValueError
    except (ValidationError, ValueError, UnicodeError):
        raise OrderRejected(503, "SERVICE_UNAVAILABLE", "Consent unavailable") from None
    return result


def create_order(
    session: Session,
    request: OrderCreate,
    headers: OrderHeaders,
    security: OrderSecurity,
) -> OrderReceipt:
    if request.source != "site":
        raise OrderRejected(422, "VALIDATION_ERROR", "Invalid request parameters")
    consent_text = effective_consent(session, lock=True)
    if request.consent.version != consent_text.version:
        raise OrderRejected(409, "CONSENT_CHANGED", "Consent has changed")

    selections = {item.item_id: item for item in request.items}
    # One PostgreSQL statement locks rows in UUID order, independent of input order.
    catalog = (
        list(
            session.scalars(
                select(CatalogItem)
                .where(CatalogItem.id.in_(selections))
                .order_by(CatalogItem.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if selections
        else []
    )
    if len(catalog) != len(selections) or any(
        item.status != "published"
        or item.revision != selections[item.id].expected_revision
        for item in catalog
    ):
        raise OrderRejected(409, "CATALOG_CHANGED", "Catalog has changed")

    known_subtotal = ZERO
    unknown = request.kind == "support"
    shipping = False
    for item in catalog:
        if item.price_mode == "on_request":
            unknown = True
        else:
            price = item.price
            if (
                price is None
                or not price.is_finite()
                or not ZERO <= price <= MAX_AMOUNT
            ):
                raise OrderRejected(422, "VALIDATION_ERROR", "Invalid order amounts")
            known_subtotal += price * selections[item.id].qty
        shipping |= item.kind == "product" and item.requires_shipping
    if known_subtotal > MAX_AMOUNT:
        raise OrderRejected(422, "VALIDATION_ERROR", "Invalid order amounts")
    items_total = None if unknown else known_subtotal
    delivery_cost = None if shipping else ZERO
    total = items_total if delivery_cost is not None else None

    # Catalog locks may have waited past a scheduled consent retirement/activation.
    # Recheck the effective version immediately before creating any evidence.
    if effective_consent(session, lock=True).version != request.consent.version:
        raise OrderRejected(409, "CONSENT_CHANGED", "Consent has changed")
    consent = Consent(text_version=consent_text.version)
    session.add(consent)
    session.flush()
    order = Order(
        kind=request.kind,
        organization_name=request.organization_name,
        contact_name=request.contact.name,
        email=request.contact.email,
        phone=request.contact.phone,
        city=request.city,
        comment=request.comment,
        source="site",
        status="new",
        version=1,
        consent_id=consent.id,
        idempotency_key=headers.idempotency_key,
        request_hash=security.request_hash(request),
        receipt_token_hash=token_hash(headers.request_token),
        known_subtotal=known_subtotal,
        items_total=items_total,
        delivery_status="pending_quote" if shipping else "not_required",
        delivery_cost=delivery_cost,
        total_amount=total,
        currency="RUB",
    )
    session.add(order)
    session.flush()
    for item in catalog:
        session.add(
            OrderItem(
                order_id=order.id,
                item_id=item.id,
                qty=selections[item.id].qty,
                sku_snapshot=item.sku,
                name_snapshot=item.name,
                kind_snapshot=item.kind,
                unit_snapshot=item.unit,
                price_mode_snapshot=item.price_mode,
                unit_price_snapshot=item.price,
                currency_snapshot=item.currency,
                tax_note_snapshot=item.tax_note,
                requires_shipping_snapshot=item.requires_shipping,
                catalog_revision_snapshot=item.revision,
            )
        )
    session.flush()
    session.add(
        OrderStatusHistory(
            order_id=order.id, from_status=None, to_status="new", changed_by=None
        )
    )
    session.flush()
    session.add(
        EmailOutbox(
            order_id=order.id, event_kind="manager_notification", status="pending"
        )
    )
    session.flush()
    return OrderReceipt(
        number=order.number,
        status="new",
        known_subtotal=known_subtotal,
        items_total=items_total,
        delivery_status="pending_quote" if shipping else "not_required",
        delivery_cost=delivery_cost,
        total_amount=total,
        currency="RUB",
    )
