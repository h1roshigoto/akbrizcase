"""Staff commands share the existing UoW; no service commits or rolls back."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import exists, select, text, update
from sqlalchemy.orm import Session

from app.core.audit import StaffAction, StaffResult, record_staff_event
from app.core.auth import (
    StaffRole,
    authenticate,
    has_role,
    hash_password,
    valid_login,
    valid_password,
)
from app.core.db_types import utc_now
from app.core.staff_models import AuditEvent, StaffSession, StaffUser

# A public lock key is coordination, never an authentication secret. Serialize
# management/auth checks so a concurrent disable cannot race a privileged write.
_STAFF_LOCK = 4004


@dataclass(frozen=True)
class StaffOutcome:
    result: StaffResult
    user_id: UUID | None = None

    @property
    def ok(self) -> bool:
        return self.result == "success"


def lock_staff(session: Session) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAFF_LOCK})


def _finish(
    session: Session,
    action: StaffAction,
    result: StaffResult,
    actor: StaffUser | None = None,
    target: StaffUser | None = None,
    role: StaffRole | None = None,
    is_active: bool | None = None,
) -> StaffOutcome:
    record_staff_event(
        session,
        actor_id=actor.id if actor else None,
        action=action,
        result=result,
        entity_id=target.id if target else None,
        role=role,
        is_active=is_active,
    )
    return StaffOutcome(result, target.id if target else None)


def bootstrap_admin(session: Session, *, login: str, password: str) -> StaffOutcome:
    """Protected operator-only first setup, permanently closed after success.

    The advisory lock and immutable unique success audit marker live in the same
    PostgreSQL transaction as the user. Inactive admins also close bootstrap.
    """
    lock_staff(session)
    if session.scalar(
        select(exists().where(StaffUser.role == "admin"))
    ) or session.scalar(
        select(
            exists().where(
                AuditEvent.action == "staff.bootstrap", AuditEvent.result == "success"
            )
        )
    ):
        return _finish(session, "staff.bootstrap", "bootstrap_closed")
    if not valid_login(login) or not valid_password(password):
        return _finish(session, "staff.bootstrap", "invalid_input")
    if session.scalar(select(exists().where(StaffUser.login == login))):
        return _finish(session, "staff.bootstrap", "login_exists")
    user = StaffUser(login=login, password_hash=hash_password(password), role="admin")
    session.add(user)
    session.flush()
    return _finish(session, "staff.bootstrap", "success", user, user, "admin", True)


def create_staff(
    session: Session,
    *,
    admin_login: str,
    admin_password: str,
    login: str,
    password: str,
    role: StaffRole,
) -> StaffOutcome:
    lock_staff(session)
    actor = authenticate(session, admin_login, admin_password)
    if actor is None:
        return _finish(session, "staff.create", "authentication_denied")
    if not has_role(actor, "admin"):
        return _finish(session, "staff.create", "forbidden", actor)
    if (
        role not in ("manager", "admin")
        or not valid_login(login)
        or not valid_password(password)
    ):
        return _finish(session, "staff.create", "invalid_input", actor)
    if session.scalar(select(exists().where(StaffUser.login == login))):
        return _finish(session, "staff.create", "login_exists", actor)
    user = StaffUser(login=login, password_hash=hash_password(password), role=role)
    session.add(user)
    session.flush()
    return _finish(session, "staff.create", "success", actor, user, role, True)


def disable_staff(
    session: Session,
    *,
    admin_login: str,
    admin_password: str,
    user_id: UUID,
) -> StaffOutcome:
    lock_staff(session)
    actor = authenticate(session, admin_login, admin_password)
    if actor is None:
        return _finish(session, "staff.disable", "authentication_denied")
    if not has_role(actor, "admin"):
        return _finish(session, "staff.disable", "forbidden", actor)
    user = session.get(StaffUser, user_id)
    if user is None:
        return _finish(session, "staff.disable", "not_found", actor)
    user.is_active = False
    session.execute(
        update(StaffSession)
        .where(StaffSession.user_id == user.id, StaffSession.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )
    return _finish(session, "staff.disable", "success", actor, user, is_active=False)
