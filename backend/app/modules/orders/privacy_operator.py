"""Authenticated publication. Only the outer UoW commits or rolls back."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.core.auth import has_role
from app.core.privacy import load_bundle
from app.core.staff import authenticate_operator
from app.core.staff_models import AuditEvent
from app.modules.orders.models import ConsentText
from app.modules.orders.privacy_models import ConsentPublication
from app.modules.orders.privacy_service import lock_privacy, synthetic_database


@dataclass(frozen=True)
class PrivacyOutcome:
    code: str

    @property
    def ok(self) -> bool:
        return self.code == "SUCCESS"


def run_privacy(
    session: Session,
    *,
    login: str,
    password: str,
    operation: Literal["publish", "retire"],
    expected_version: str | None,
    path: Path | None = None,
    expected_checksum: str | None = None,
) -> PrivacyOutcome:
    actor = authenticate_operator(session, login, password)

    def finish(code: str) -> PrivacyOutcome:
        session.add(
            AuditEvent(
                actor_id=actor.user.id if actor else None,
                action=f"privacy.{operation}",
                entity_type="consent_publication",
                result=code.lower(),
                details={},
            )
        )
        session.flush()
        return PrivacyOutcome(code)

    if actor is None:
        return finish("AUTHENTICATION_DENIED")
    if not has_role(actor.user, "admin"):
        return finish("FORBIDDEN")
    # Privilege boundary: ordinary API credentials cannot publish even with admin ID.
    if session.scalar(select(func.current_user())) != "akbrizcase_migrator":
        return finish("PROTECTED_DATABASE_ROLE_REQUIRED")
    lock_privacy(session, shared=False)
    session.execute(text("LOCK TABLE app.consent_texts IN SHARE ROW EXCLUSIVE MODE"))
    now = session.scalar(select(func.clock_timestamp()))
    assert now is not None
    rows = list(
        session.scalars(
            select(ConsentText)
            .where(or_(ConsentText.retired_at.is_(None), ConsentText.retired_at > now))
            .order_by(ConsentText.version)
            .with_for_update()
        )
    )
    # Reject future/scheduled records as well as overlaps. No guessed clock or
    # retroactive retirement, no rewriting an already retired version.
    if len(rows) > 1 or any(row.active_from > now for row in rows):
        return finish("STATE_CONFLICT")
    current = rows[0] if rows else None
    if (current.version if current else None) != expected_version:
        return finish("STATE_CONFLICT")
    if operation == "retire":
        if (
            current is None
            or current.retired_at is not None
            or now <= current.active_from
        ):
            return finish("STATE_CONFLICT")
        current.retired_at = now
        return finish("SUCCESS")
    try:
        if path is None:
            raise ValueError
        bundle = load_bundle(path)
        if (
            bundle.checksum() != expected_checksum
            or not bundle.valid_at(now)
            or (
                bundle.approval_status == "SYNTHETIC ONLY"
                and not synthetic_database(session)
            )
        ):
            raise ValueError
    except (OSError, ValueError, UnicodeError):
        return finish("INVALID_APPROVAL_PACKET")
    if session.get(ConsentText, bundle.consent.version) is not None:
        return finish("VERSION_EXISTS")
    for previous in session.scalars(select(ConsentPublication)):
        old_policy = previous.bundle.get("policy")
        if (
            isinstance(old_policy, dict)
            and old_policy.get("version") == bundle.policy.version
        ):
            if old_policy != bundle.policy.model_dump(mode="json"):
                return finish("POLICY_VERSION_EXISTS")
    if current is not None:
        if current.retired_at is not None or now <= current.active_from:
            return finish("STATE_CONFLICT")
        current.retired_at = now
    session.add(ConsentText(**bundle.consent.model_dump(), active_from=now))
    session.flush()
    session.add(
        ConsentPublication(
            consent_version=bundle.consent.version,
            bundle_checksum=bundle.checksum(),
            bundle=bundle.model_dump(mode="json"),
            published_at=now,
        )
    )
    session.flush()
    return finish("SUCCESS")
