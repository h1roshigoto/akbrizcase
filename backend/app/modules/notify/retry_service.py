"""Manual failed → pending transition under the existing staff Unit of Work."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_notification_retry_event
from app.core.auth import has_role
from app.core.staff_sessions import StaffIdentity
from app.modules.notify.models import EmailOutbox
from app.modules.notify.outbox import database_now
from app.modules.orders.service import OrderRejected


def retry_failed_notification(
    session: Session, order_id: UUID, actor: StaffIdentity
) -> None:
    """Refresh after locking; flush atomically, never commit or perform SMTP."""
    if not has_role(actor.user, "manager"):
        raise OrderRejected(403, "FORBIDDEN", "Insufficient permissions")
    with session.begin_nested():
        row = session.scalar(
            select(EmailOutbox)
            .where(
                EmailOutbox.order_id == order_id,
                EmailOutbox.event_kind == "manager_notification",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        # The existing FK ensures a retained outbox belongs to an existing order.
        if row is None:
            raise OrderRejected(404, "NOT_FOUND", "Object not found")
        if row.status != "failed":
            raise OrderRejected(
                409, "STATE_CONFLICT", "Notification changed; reload current order"
            )
        row.status = "pending"
        row.attempts = 0
        row.next_attempt_at = database_now(session)
        row.lease_token = None
        row.lease_until = None
        row.last_error_code = None
        session.flush()
        record_notification_retry_event(
            session, actor_id=actor.user.id, outbox_id=row.id
        )
