"""Staff audit allowlist: never accept arbitrary payloads, logins or secrets."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.auth import StaffRole
from app.core.staff_models import AuditEvent

StaffAction = Literal[
    "staff.bootstrap",
    "staff.create",
    "staff.disable",
    "staff.login",
    "staff.logout",
    "staff.access",
    "staff.csrf",
]
StaffResult = Literal[
    "success",
    "authentication_denied",
    "forbidden",
    "bootstrap_closed",
    "invalid_input",
    "login_exists",
    "not_found",
    "csrf_denied",
    "origin_denied",
]


def record_staff_event(
    session: Session,
    *,
    actor_id: UUID | None,
    action: StaffAction,
    result: StaffResult,
    entity_id: UUID | None = None,
    role: StaffRole | None = None,
    is_active: bool | None = None,
) -> None:
    details: dict[str, str | bool] = {}
    if role is not None:
        details["role"] = role
    if is_active is not None:
        details["is_active"] = is_active
    session.add(
        AuditEvent(
            actor_id=actor_id,
            action=action,
            entity_type="staff_user",
            entity_id=entity_id,
            result=result,
            details=details,
        )
    )
    session.flush()


def record_order_status_event(
    session: Session,
    *,
    actor_id: UUID,
    order_id: UUID,
    from_status: str,
    to_status: str,
    from_version: int,
    to_version: int,
    at: datetime,
) -> None:
    """Fixed technical metadata only; free-text reasons never enter the audit."""
    session.add(
        AuditEvent(
            actor_id=actor_id,
            action="order.status.change",
            entity_type="order",
            entity_id=order_id,
            result="success",
            at=at,
            details={
                "from_status": from_status,
                "to_status": to_status,
                "from_version": from_version,
                "to_version": to_version,
            },
        )
    )
    session.flush()


def record_notification_retry_event(
    session: Session, *, actor_id: UUID, outbox_id: UUID
) -> None:
    """Fixed operation metadata; accepts no request content or SMTP diagnostics."""
    session.add(
        AuditEvent(
            actor_id=actor_id,
            action="notification.retry",
            entity_type="email_outbox",
            entity_id=outbox_id,
            result="success",
            details={"from_status": "failed", "to_status": "pending"},
        )
    )
    session.flush()
