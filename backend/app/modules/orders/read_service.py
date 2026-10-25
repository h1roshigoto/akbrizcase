"""Read-only PostgreSQL projections shared by SQLAdmin and protected GET APIs.

The caller owns the staff UoW. No catalog lookups, email delivery or ORM writes.
"""

from uuid import UUID

from sqlalchemy import case, func, select, true
from sqlalchemy.orm import Session

from app.modules.notify.models import EmailOutbox
from app.modules.orders.models import (
    Consent,
    ConsentText,
    Order,
    OrderItem,
    OrderStatusHistory,
)
from app.schemas.order_reads import (
    ConsentEvidence,
    ItemSnapshot,
    OrderDetail,
    OrderPage,
    OrderReadQuery,
    OrderSummary,
    StatusEvent,
)

PERSONAL_FIELDS = frozenset(
    {"contact_name", "email", "phone", "organization_name", "city", "comment"}
)
DETAIL_FIELDS = (
    *OrderSummary.model_fields,
    "source",
    "version",
    "updated_at",
    "closed_at",
    "pii_erased_at",
    *sorted(PERSONAL_FIELDS),
)


class OrderNotFound(Exception):
    pass


class OrderReadService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self, query: OrderReadQuery) -> OrderPage:
        filters = () if query.status is None else (Order.status == query.status,)
        page = (
            select(*(getattr(Order, field) for field in OrderSummary.model_fields))
            .where(*filters)
            .order_by(Order.created_at.desc(), Order.number.desc())
            .limit(query.limit)
            .offset(query.offset)
            .cte("bounded_orders")
        )
        total = (
            select(func.count().label("_total"))
            .select_from(Order)
            .where(*filters)
            .subquery()
        )
        # A single statement gives count and page the same PostgreSQL snapshot,
        # including when a concurrent intake commits between logical reads.
        # LEFT JOIN retains the count for empty/out-of-range pages.
        rows = (
            self.session.execute(
                select(total.c._total, *page.c)
                .select_from(total.outerjoin(page, true()))
                .order_by(page.c.created_at.desc(), page.c.number.desc())
            )
            .mappings()
            .all()
        )
        return OrderPage(
            items=[
                OrderSummary.model_validate(
                    {field: row[field] for field in OrderSummary.model_fields}
                )
                for row in rows
                if row["id"] is not None
            ],
            total=rows[0]["_total"],
            limit=query.limit,
            offset=query.offset,
        )

    def detail(self, identifier: UUID) -> OrderDetail:
        # SHARE serializes the projection against erasure/status updates until
        # the caller's UoW completes. Do not select replay verification columns.
        row = (
            self.session.execute(
                select(
                    *(
                        case(
                            (Order.pii_erased_at.is_(None), getattr(Order, field)),
                            else_=None,
                        ).label(field)
                        if field in PERSONAL_FIELDS
                        else getattr(Order, field)
                        for field in DETAIL_FIELDS
                    ),
                    Order.consent_id,
                )
                .where(Order.id == identifier)
                .with_for_update(read=True)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise OrderNotFound
        values = dict(row)
        consent_id = values.pop("consent_id")
        consent = (
            self.session.execute(
                select(
                    Consent.id,
                    Consent.text_version,
                    Consent.granted_at,
                    Consent.channel,
                    Consent.revoked_at,
                    ConsentText.purpose,
                    ConsentText.checksum,
                )
                .join(ConsentText, ConsentText.version == Consent.text_version)
                .where(Consent.id == consent_id)
            )
            .mappings()
            .one()
        )
        items = self.session.execute(
            select(*(getattr(OrderItem, field) for field in ItemSnapshot.model_fields))
            .where(OrderItem.order_id == identifier)
            .order_by(OrderItem.sku_snapshot, OrderItem.id)
        ).mappings()
        history = self.session.execute(
            select(
                *(
                    case(
                        (Order.pii_erased_at.is_(None), OrderStatusHistory.reason),
                        else_=None,
                    ).label("reason")
                    if field == "reason"
                    else getattr(OrderStatusHistory, field)
                    for field in StatusEvent.model_fields
                )
            )
            .join(Order, Order.id == OrderStatusHistory.order_id)
            .where(OrderStatusHistory.order_id == identifier)
            .order_by(OrderStatusHistory.at, OrderStatusHistory.id)
        ).mappings()
        notification = self.session.scalar(
            select(EmailOutbox.status).where(
                EmailOutbox.order_id == identifier,
                EmailOutbox.event_kind == "manager_notification",
            )
        )
        return OrderDetail.model_validate(
            values
            | {
                "consent": ConsentEvidence.model_validate(dict(consent)),
                "items": [ItemSnapshot.model_validate(dict(item)) for item in items],
                "history": [
                    StatusEvent.model_validate(dict(entry)) for entry in history
                ],
                "notification_status": notification,
            }
        )
