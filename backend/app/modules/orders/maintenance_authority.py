"""Transaction-scoped authority issued only by protected staff authentication."""

from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import has_role
from app.core.staff import authenticate_operator
from app.core.staff_models import AuditEvent
from app.modules.orders.privacy_service import synthetic_database

_AUTHORITY_KEY = object()


def has_audit_authority(session: Session) -> bool:
    transaction = session.get_transaction()
    return transaction is not None and session.info.get(_AUTHORITY_KEY) is transaction


def authorize_maintenance(
    session: Session,
    login: str,
    password: str,
    *,
    action: Literal["privacy.audit_maintenance", "privacy.retention_review"],
) -> str:
    session.info.pop(_AUTHORITY_KEY, None)
    actor = authenticate_operator(session, login, password)
    if actor is None:
        code = "AUTHENTICATION_DENIED"
    elif not has_role(actor.user, "admin"):
        code = "FORBIDDEN"
    elif session.scalar(select(func.current_user())) != "akbrizcase_migrator":
        code = "PROTECTED_DATABASE_ROLE_REQUIRED"
    elif not synthetic_database(session):
        code = "SYNTHETIC_DATABASE_REQUIRED"
    else:
        code = "SUCCESS"
    if code == "SUCCESS" and action == "privacy.audit_maintenance":
        session.info[_AUTHORITY_KEY] = session.get_transaction()
    session.add(
        AuditEvent(
            actor_id=actor.user.id if actor else None,
            action=action,
            entity_type="retention_maintenance",
            result=code.lower(),
            details={},
        )
    )
    session.flush()
    return code
