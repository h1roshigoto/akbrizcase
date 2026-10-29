"""Fail-closed document gates and commit-scoped publication coordination."""

import os

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.privacy import PrivacyBundle, production_bundle
from app.modules.orders.privacy_models import ConsentPublication

PRIVACY_LOCK = 4037


class PrivacyUnavailable(ValueError):
    pass


def lock_privacy(session: Session, *, shared: bool) -> None:
    operation = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    session.execute(text(f"SELECT {operation}(:key)"), {"key": PRIVACY_LOCK})


def synthetic_database(session: Session) -> bool:
    mode = os.environ.get("APP_ENV")
    if mode not in {"demo", "test"}:
        return False
    marker = session.scalar(
        text(
            "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
            "WHERE datname = current_database()"
        )
    )
    return bool(marker == f"akbrizcase:synthetic-seed:{mode}")


def approved_publication(
    session: Session, version: str, checksum: str
) -> PrivacyBundle:
    try:
        publication = session.get(ConsentPublication, version, populate_existing=True)
        if publication is None:
            raise ValueError
        bundle = PrivacyBundle.model_validate(publication.bundle)
        now = session.scalar(select(func.clock_timestamp()))
        if (
            bundle.consent.version != version
            or bundle.consent.checksum != checksum
            or bundle.checksum() != publication.bundle_checksum
            or now is None
            or not bundle.valid_at(now)
        ):
            raise ValueError
        if bundle.approval_status == "SYNTHETIC ONLY":
            if not synthetic_database(session):
                raise ValueError
        elif production_bundle().checksum() != publication.bundle_checksum:
            raise ValueError
        return bundle
    except (ValueError, UnicodeError):
        raise PrivacyUnavailable("Approved privacy documents unavailable") from None
