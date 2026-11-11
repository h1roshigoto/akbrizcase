"""Staff commands share the existing UoW; no service commits or rolls back."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import exists, select, text, update
from sqlalchemy.orm import Session, object_session

from app.core.audit import StaffAction, StaffResult, record_staff_event
from app.core.auth import (
    AuthenticatedStaff,
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


def authenticate_operator(
    session: Session, login: str, password: str
) -> AuthenticatedStaff | None:
    """Verify Argon2id under the same commit-scoped lock as staff-disable."""
    lock_staff(session)
    user = authenticate(session, login, password)
    return AuthenticatedStaff(user) if user is not None else None


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
    actor = authenticate_operator(session, admin_login, admin_password)
    if actor is None:
        return _finish(session, "staff.create", "authentication_denied")
    return create_staff_as_actor(
        session, actor=actor, login=login, password=password, role=role
    )


def _current_actor(session: Session, actor: AuthenticatedStaff) -> StaffUser | None:
    """Refresh a verified identity in its authenticating UoW under staff lock.

    This is an internal adapter contract, never an actor ID from an HTTP form.
    Browser identities additionally require their still-live database session.
    """
    from app.core.staff_sessions import StaffIdentity

    lock_staff(session)
    if object_session(actor.user) is not session:
        return None
    session.refresh(actor.user)
    if not actor.user.is_active:
        return None
    if isinstance(actor, StaffIdentity):
        if object_session(actor.session) is not session:
            return None
        session.refresh(actor.session)
        if (
            actor.session.user_id != actor.user.id
            or actor.session.revoked_at is not None
            or actor.session.expires_at <= utc_now()
        ):
            return None
    return actor.user


def create_staff_as_actor(
    session: Session,
    *,
    actor: AuthenticatedStaff,
    login: str,
    password: str,
    role: StaffRole,
) -> StaffOutcome:
    """Create through verified CLI/browser authority; caller owns the UoW."""
    current = _current_actor(session, actor)
    if current is None:
        return _finish(session, "staff.create", "authentication_denied")
    if not has_role(current, "admin"):
        return _finish(session, "staff.create", "forbidden", current)
    if (
        role not in ("manager", "admin")
        or not valid_login(login)
        or not valid_password(password)
    ):
        return _finish(session, "staff.create", "invalid_input", current)
    if session.scalar(select(exists().where(StaffUser.login == login))):
        return _finish(session, "staff.create", "login_exists", current)
    user = StaffUser(login=login, password_hash=hash_password(password), role=role)
    session.add(user)
    session.flush()
    return _finish(session, "staff.create", "success", current, user, role, True)


def disable_staff(
    session: Session,
    *,
    admin_login: str,
    admin_password: str,
    user_id: UUID,
) -> StaffOutcome:
    actor = authenticate_operator(session, admin_login, admin_password)
    if actor is None:
        return _finish(session, "staff.disable", "authentication_denied")
    return disable_staff_as_actor(session, actor=actor, user_id=user_id)


def disable_staff_as_actor(
    session: Session, *, actor: AuthenticatedStaff, user_id: UUID
) -> StaffOutcome:
    """Preserve CLI policy, including self/last-admin disable and atomic revoke."""
    current = _current_actor(session, actor)
    if current is None:
        return _finish(session, "staff.disable", "authentication_denied")
    if not has_role(current, "admin"):
        return _finish(session, "staff.disable", "forbidden", current)
    user = session.get(StaffUser, user_id)
    if user is None:
        return _finish(session, "staff.disable", "not_found", current)
    user.is_active = False
    session.execute(
        update(StaffSession)
        .where(StaffSession.user_id == user.id, StaffSession.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )
    return _finish(session, "staff.disable", "success", current, user, is_active=False)
