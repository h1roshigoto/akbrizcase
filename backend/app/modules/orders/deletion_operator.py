"""Authenticated admin + migrator boundary; no identity documents are inputs."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import has_role
from app.core.staff import authenticate_operator
from app.core.staff_models import AuditEvent
from app.modules.orders.cleanup_service import (
    database_now,
    order_policy,
    prepare_entry,
    try_lock,
)
from app.modules.orders.deletion_journal import DeletionCommand, command_for
from app.modules.orders.models import Order


def authorize(session: Session, login: str, password: str) -> str:
    actor = authenticate_operator(session, login, password)
    if actor is None:
        code = "AUTHENTICATION_DENIED"
    elif not has_role(actor.user, "admin"):
        code = "FORBIDDEN"
    elif session.scalar(select(func.current_user())) != "akbrizcase_migrator":
        code = "PROTECTED_DATABASE_ROLE_REQUIRED"
    else:
        code = "SUCCESS"
    session.add(
        AuditEvent(
            actor_id=actor.user.id if actor else None,
            action="privacy.order_delete",
            entity_type="deletion_operation",
            result=code.lower(),
            details={},
        )
    )
    session.flush()
    return code


def preflight(
    session: Session, *, login: str, password: str, order_id: UUID
) -> tuple[str, int | None]:
    code = authorize(session, login, password)
    if code != "SUCCESS":
        return code, None
    row = session.get(Order, order_id)
    if row is None:
        # No receipt or distinction disclosing a previously deleted target.
        return "ABSENT", None
    return "READY", row.version


def prepare_deletion(
    session: Session,
    *,
    login: str,
    password: str,
    order_id: UUID,
    expected_version: int,
    decision_id: UUID,
    now: datetime | None = None,
) -> tuple[str, DeletionCommand | None]:
    code = authorize(session, login, password)
    if code != "SUCCESS":
        return code, None
    if not try_lock(session):
        return "BUSY", None
    row = session.scalar(
        select(Order)
        .where(Order.id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return "ABSENT", None
    if row.version != expected_version:
        return "STATE_CONFLICT", None
    at = database_now(session, now)
    bundle = order_policy(session, row, at)
    entry = prepare_entry(
        session,
        "order_delete",
        row.id,
        bundle,
        at,
        consent_id=row.consent_id,
        decision_id=decision_id,
    )
    if entry.decision_id != decision_id:
        return "DECISION_CONFLICT", None
    return "PREPARED", command_for(entry)
