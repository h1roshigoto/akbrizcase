"""Database-backed authority shared by HTTP and SQLAdmin; caller owns the UoW."""

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_staff_event
from app.core.auth import AuthenticatedStaff, authenticate
from app.core.db_types import utc_now
from app.core.staff import lock_staff
from app.core.staff_models import StaffSession, StaffUser

SESSION_SECONDS = 8 * 60 * 60


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_token(token: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{64}", token) is not None


@dataclass(frozen=True)
class StaffIdentity(AuthenticatedStaff):
    session: StaffSession


@dataclass(frozen=True)
class IssuedSession:
    identity: StaffIdentity
    token: str = field(repr=False)


def resolve_session(session: Session, token: str) -> StaffIdentity | None:
    """Must run under lock_staff, held through the protected operation's commit.

    Always refresh both records, including when a caller reuses an ORM session.
    Never persist raw tokens or slide the absolute expiry.
    """
    if not valid_token(token):
        return None
    row = session.execute(
        select(StaffUser, StaffSession)
        .join(StaffSession, StaffSession.user_id == StaffUser.id)
        .where(
            StaffSession.token_hash == token_hash(token),
            StaffSession.revoked_at.is_(None),
            StaffSession.expires_at > utc_now(),
            StaffUser.is_active.is_(True),
            StaffUser.role.in_(("manager", "admin")),
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    return StaffIdentity(row[0], row[1]) if row else None


def login_staff(session: Session, login: str, password: str) -> IssuedSession | None:
    lock_staff(session)
    user = authenticate(session, login, password)
    if user is None:
        record_staff_event(
            session, actor_id=None, action="staff.login", result="authentication_denied"
        )
        return None
    token = secrets.token_hex(32)
    row = StaffSession(
        user_id=user.id,
        token_hash=token_hash(token),
        expires_at=utc_now() + timedelta(seconds=SESSION_SECONDS),
    )
    session.add(row)
    session.flush()
    record_staff_event(
        session, actor_id=user.id, action="staff.login", result="success"
    )
    return IssuedSession(StaffIdentity(user, row), token)


def logout_staff(session: Session, identity: StaffIdentity) -> None:
    identity.session.revoked_at = utc_now()
    record_staff_event(
        session, actor_id=identity.user.id, action="staff.logout", result="success"
    )
