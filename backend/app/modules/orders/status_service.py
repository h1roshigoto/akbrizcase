"""Locked status changes; the existing staff UoW commits before HTTP delivery."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only

from app.core.audit import record_order_status_event
from app.core.auth import has_role
from app.core.staff_sessions import StaffIdentity
from app.modules.orders.models import Order, OrderStatusHistory
from app.modules.orders.service import OrderRejected
from app.schemas.admin import OrderStatusChange, OrderStatusResult
from app.schemas.common import OrderStatus

TRANSITIONS: dict[str, tuple[OrderStatus, ...]] = {
    "new": ("in_progress", "cancelled"),
    "in_progress": ("closed", "cancelled"),
    "closed": (),
    "cancelled": (),
}


def change_status(
    session: Session,
    order_id: UUID,
    change: OrderStatusChange,
    actor: StaffIdentity,
) -> OrderStatusResult:
    """Actor is resolved by the staff boundary, under its authority lock.

    Never commit here. The savepoint also prevents partial writes if the HTTP
    error handler converts a database exception into an ordinary 503 response.
    Only the caller's existing outer staff UoW may publish a successful result.
    """
    if not has_role(actor.user, "manager"):
        raise OrderRejected(403, "FORBIDDEN", "Insufficient permissions")
    with session.begin_nested():
        order = session.scalar(
            select(Order)
            .where(Order.id == order_id)
            .options(load_only(Order.status, Order.version, raiseload=True))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if order is None:
            raise OrderRejected(404, "NOT_FOUND", "Object not found")
        if order.version != change.expected_version:
            raise OrderRejected(
                409, "STATE_CONFLICT", "Order changed; reload its current state"
            )
        if change.status not in TRANSITIONS[order.status]:
            raise OrderRejected(
                409, "STATE_CONFLICT", "Status transition is not allowed"
            )
        # Validate at the service boundary too, including trusted non-HTTP callers.
        change = OrderStatusChange.model_validate(change.model_dump())
        now = session.scalar(select(func.clock_timestamp()))
        assert now is not None
        previous_status, previous_version = order.status, order.version
        order.status = change.status
        order.version += 1
        order.updated_at = now
        if change.status == "closed":
            order.closed_at = now
        session.add(
            OrderStatusHistory(
                order_id=order.id,
                from_status=previous_status,
                to_status=change.status,
                changed_by=actor.user.id,
                reason=change.reason,
                at=now,
            )
        )
        session.flush()
        record_order_status_event(
            session,
            actor_id=actor.user.id,
            order_id=order.id,
            from_status=previous_status,
            to_status=change.status,
            from_version=previous_version,
            to_version=order.version,
            at=now,
        )
        return OrderStatusResult(status=change.status, version=order.version)
