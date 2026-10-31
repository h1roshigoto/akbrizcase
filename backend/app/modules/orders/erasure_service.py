"""Privacy mutations share the caller's UoW and never perform external I/O."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.modules.orders.models import Consent, Order, OrderStatusHistory

PII_FIELDS = (
    "contact_name",
    "email",
    "phone",
    "organization_name",
    "city",
    "comment",
    "chat_session_id",
    "request_hash",
    "receipt_token_hash",
)


def pii_erasure_complete(session: Session, order_id: UUID) -> bool:
    row = session.get(Order, order_id)
    return row is None or (
        row.pii_erased_at is not None
        and all(getattr(row, field) is None for field in PII_FIELDS)
        and session.scalar(
            select(OrderStatusHistory.id)
            .where(
                OrderStatusHistory.order_id == row.id,
                OrderStatusHistory.reason.is_not(None),
            )
            .limit(1)
        )
        is None
    )


def erase_order_pii(session: Session, order_id: UUID, now: datetime) -> bool:
    """Caller verifies policy and write-ahead witness before entering here."""
    with session.begin_nested():
        row = session.scalar(
            select(Order)
            .where(Order.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or row.pii_erased_at is not None:
            return False
        session.execute(
            update(OrderStatusHistory)
            .where(OrderStatusHistory.order_id == row.id)
            .values(reason=None)
        )
        for field in PII_FIELDS:
            setattr(row, field, None)
        row.pii_erased_at = now
        row.updated_at = now
        session.flush()
        return True


def delete_order_graph(session: Session, order_id: UUID, consent_id: UUID) -> bool:
    """Existing parent CASCADE is the sole permitted snapshot deletion path."""
    with session.begin_nested():
        row = session.scalar(
            select(Order).where(Order.id == order_id).with_for_update()
        )
        if row is None:
            return False
        if row.consent_id != consent_id:
            raise ValueError("Deletion target changed")
        session.execute(delete(Order).where(Order.id == row.id))
        session.execute(delete(Consent).where(Consent.id == consent_id))
        session.flush()
        return True
