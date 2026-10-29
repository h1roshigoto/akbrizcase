"""Separate immutable public document reads from authorization of new collection."""

import os
from datetime import datetime

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.core.privacy import PrivacyBundle, digest, production_bundle
from app.modules.orders.models import ConsentText
from app.modules.orders.privacy_models import ConsentPublication
from app.schemas.consent import ConsentText as ConsentTextDTO

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


def validated_publication(
    session: Session,
    publication: ConsentPublication,
    now: datetime,
) -> tuple[PrivacyBundle, ConsentText]:
    """Trust only the immutable packet and its matching, intact consent text."""
    try:
        row = session.get(
            ConsentText, publication.consent_version, populate_existing=True
        )
        if row is None:
            raise ValueError
        bundle = PrivacyBundle.model_validate(publication.bundle)
        actual = ConsentTextDTO(
            version=row.version,
            purpose=row.purpose,
            text=row.text,
            checksum=row.checksum,
        )
        if (
            bundle.consent != actual
            or digest(row.text) != row.checksum
            or bundle.checksum() != publication.bundle_checksum
            or publication.published_at > now
            or row.active_from > publication.published_at
            or not bundle.valid_at(publication.published_at)
            or (row.retired_at is not None and row.retired_at <= row.active_from)
        ):
            raise ValueError
        if bundle.approval_status == "SYNTHETIC ONLY" and not synthetic_database(
            session
        ):
            raise ValueError
        return bundle, row
    except (ValueError, UnicodeError):
        raise PrivacyUnavailable("Approved privacy documents unavailable") from None


def configuration_matches(bundle: PrivacyBundle) -> bool:
    if bundle.approval_status == "SYNTHETIC ONLY":
        # Marker and APP_ENV are checked by validated_publication, never by a flag.
        return True
    try:
        return production_bundle().checksum() == bundle.checksum()
    except ValueError:
        return False


def latest_publication(session: Session) -> ConsentPublication:
    newest = list(
        session.scalars(
            select(ConsentPublication)
            .order_by(ConsentPublication.published_at.desc())
            .limit(2)
            .execution_options(populate_existing=True)
        )
    )
    if not newest or (
        len(newest) == 2 and newest[0].published_at == newest[1].published_at
    ):
        raise PrivacyUnavailable("Policy publication unavailable or ambiguous")
    return newest[0]


def approved_publication(
    session: Session, version: str, checksum: str
) -> PrivacyBundle:
    """Strict write gate. Read availability must never substitute for this check."""
    publication = latest_publication(session)
    if publication.consent_version != version:
        raise PrivacyUnavailable("Current consent is not the latest publication")
    now = session.scalar(select(func.clock_timestamp()))
    if now is None:
        raise PrivacyUnavailable("Approved privacy documents unavailable")
    bundle, row = validated_publication(session, publication, now)
    if row.checksum != checksum or not configuration_matches(bundle):
        raise PrivacyUnavailable("Approved privacy configuration unavailable")
    return bundle


def public_policy(session: Session) -> tuple[PrivacyBundle, bool]:
    """Latest publication, including after retirement/config loss. No fallback."""
    lock_privacy(session, shared=True)
    publication = latest_publication(session)
    now = session.scalar(select(func.clock_timestamp()))
    if now is None:
        raise PrivacyUnavailable("Policy publication unavailable")
    bundle, row = validated_publication(session, publication, now)
    # Zero active consents is a supported read-only state. Conflicting/reanimated
    # older identities are not permission to silently choose an older document.
    active = list(
        session.scalars(
            select(ConsentText.version).where(
                ConsentText.active_from <= now,
                or_(ConsentText.retired_at.is_(None), ConsentText.retired_at > now),
            )
        )
    )
    if len(active) > 1 or (active and active[0] != row.version):
        raise PrivacyUnavailable("Policy publication conflicts with consent state")
    enabled = bool(active) and configuration_matches(bundle)
    return bundle, enabled
