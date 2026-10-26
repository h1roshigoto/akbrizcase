"""Shared password authentication and role rules for CLI and future HTTP auth."""

import re
import secrets
from dataclasses import dataclass
from typing import Literal

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.staff_models import StaffUser

StaffRole = Literal["manager", "admin"]


@dataclass(frozen=True)
class AuthenticatedStaff:
    """A verified database identity; password CLI does not invent a web session."""

    user: StaffUser


# Argon2id RFC 9106 low-memory profile, with independent random salts.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, type=Type.ID)
_dummy_hash = _hasher.hash(secrets.token_urlsafe(32))


def valid_login(login: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", login) is not None


def valid_password(password: str) -> bool:
    return 12 <= len(password) and len(password.encode("utf-8")) <= 1024


def hash_password(password: str) -> str:
    if not valid_password(password):
        raise ValueError(
            "Password must contain at least 12 characters and at most 1024 bytes"
        )
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if len(password.encode("utf-8")) > 1024:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def has_role(user: StaffUser, required: StaffRole) -> bool:
    return user.is_active and (
        user.role == "admin" or (required == "manager" and user.role == "manager")
    )


def authenticate(session: Session, login: str, password: str) -> StaffUser | None:
    """Never trust caller-supplied actor IDs; return a DB-backed active identity.

    Failed login/disabled/password checks have one public outcome. Unknown users
    also perform Argon2 verification. Callers own the UoW and failure auditing.
    """
    user = session.scalar(
        select(StaffUser)
        .where(StaffUser.login == login)
        .execution_options(populate_existing=True)
    )
    verified = verify_password(user.password_hash if user else _dummy_hash, password)
    if user is None or not verified or not user.is_active:
        return None
    return user
