"""Atomic creation and locked receipt replay within the caller's UoW."""

import hashlib
import hmac
import os
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session, load_only

from app.core.db_types import utc_now
from app.core.staff_sessions import token_hash
from app.modules.catalog.models import CatalogItem
from app.modules.chat.access import ChatExpired, require_owner
from app.modules.notify.models import EmailOutbox
from app.modules.orders.models import (
    Consent,
    ConsentText,
    Order,
    OrderItem,
    OrderStatusHistory,
)
from app.modules.orders.privacy_service import (
    PrivacyUnavailable,
    approved_publication,
    lock_privacy,
    synthetic_database,
)
from app.modules.orders.security import OrderSecurity
from app.modules.orders.seed import DEMO_VERSION
from app.schemas.common import OrderHeaders
from app.schemas.consent import ConsentText as ConsentTextDTO
from app.schemas.orders import OrderCreate, OrderReceipt

ZERO = Decimal("0.00")
MAX_AMOUNT = Decimal("9999999999.99")  # Existing numeric(12,2) storage.


def within_replay_window(created_at: datetime, now: datetime) -> bool:
    return now <= created_at + timedelta(hours=24)


class OrderRejected(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def replay_order(
    session: Session,
    request: OrderCreate,
    headers: OrderHeaders,
    security: OrderSecurity,
) -> OrderReceipt | None:
    # UPDATE conflicts with erasure's UPDATE/DELETE. READ COMMITTED refreshes
    # after waiting; hold the lock until the outer UoW has completed. Never load
    # contacts, comments, manager status/history or current catalog/outbox here.
    order = session.scalar(
        select(Order)
        .where(Order.idempotency_key == headers.idempotency_key)
        .options(
            load_only(
                Order.number,
                Order.pii_erased_at,
                Order.receipt_token_hash,
                Order.request_hash,
                Order.created_at,
                Order.known_subtotal,
                Order.items_total,
                Order.delivery_status,
                Order.delivery_cost,
                Order.total_amount,
                raiseload=True,
            )
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if order is None:
        return None
    submitted = token_hash(headers.request_token)
    # An ephemeral dummy digest gives erased and wrong-token paths the same
    # token-hash/compare work, without retaining any erased verification data.
    authenticated = hmac.compare_digest(order.receipt_token_hash or "0" * 64, submitted)
    if order.pii_erased_at is not None or not authenticated:
        raise OrderRejected(404, "NOT_FOUND", "Object not found")
    now = session.scalar(select(func.clock_timestamp()))
    if now is None or not within_replay_window(order.created_at, now):
        # Retain uniqueness, without promising replay after the guarantee.
        raise OrderRejected(404, "NOT_FOUND", "Object not found")
    if not security.matches_request(request, order.request_hash or ""):
        raise OrderRejected(409, "IDEMPOTENCY_CONFLICT", "Idempotency conflict")
    return OrderReceipt(
        number=order.number,
        status="new",
        known_subtotal=order.known_subtotal,
        items_total=order.items_total,
        delivery_status=(
            "not_required"
            if order.delivery_status == "not_required"
            else "pending_quote"
        ),
        delivery_cost=order.delivery_cost,
        total_amount=order.total_amount,
        currency="RUB",
    )


def effective_consent(session: Session, *, lock: bool = False) -> ConsentTextDTO:
    lock_privacy(session, shared=True)
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
    try:
        bundle = approved_publication(session, row.version, row.checksum)
        if bundle.consent != result:
            raise PrivacyUnavailable("Consent publication mismatch")
    except PrivacyUnavailable:
        raise OrderRejected(
            503, "SERVICE_UNAVAILABLE", "Privacy documents unavailable"
        ) from None
    return result


def collection_gate(session: Session) -> None:
    """Authorize new collection only; existing receipts/messages replay first."""
    if not synthetic_database(session):
        effective_consent(session, lock=True)


def create_order(
    session: Session,
    request: OrderCreate,
    headers: OrderHeaders,
    security: OrderSecurity,
    chat_token: str | None = None,
) -> OrderReceipt:
    # Only reached after replay_order. Hold the shared owner lock through commit:
    # DELETE and chat writers cannot invalidate this FK/ownership decision.
    lock_privacy(session, shared=True)
    chat = None
    if request.source == "chat":
        assert request.chat_session_id is not None  # OrderCreate contract.
        chat = require_owner(session, request.chat_session_id, chat_token)
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
    if chat is not None and chat.expires_at <= utc_now():
        raise ChatExpired
    consent = Consent(
        text_version=consent_text.version,
        granted_at=session.scalar(select(func.clock_timestamp())),
    )
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
        source=request.source,
        chat_session_id=chat.id if chat is not None else None,
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
    # Flush can itself wait on FK/UNIQUE locks. Any expiry here rolls back the
    # entire prepared write set; a UNIQUE loser still follows AQ-01 in the router.
    if chat is not None and chat.expires_at <= utc_now():
        raise ChatExpired
    if effective_consent(session, lock=True).version != request.consent.version:
        raise OrderRejected(409, "CONSENT_CHANGED", "Consent has changed")
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
