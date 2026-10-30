"""Explicit synthetic consent fixture. This is NOT legally approved language."""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.db import Database, DatabaseConfigurationError, DatabaseSettings
from app.core.privacy import PrivacyBundle, digest
from app.modules.catalog.seed import SeedRefused, validate_environment
from app.modules.orders.models import ConsentText
from app.modules.orders.privacy_models import ConsentPublication

DEMO_VERSION = "SYNTHETIC-DEMO-ONLY-v1"
DEMO_PURPOSE = "SYNTHETIC DEMO/TEST ONLY — NOT LEGALLY APPROVED"
DEMO_TEXT = (
    "SYNTHETIC DEMO/TEST CONSENT PLACEHOLDER. "
    "Not legally approved. Not for collecting real personal data."
)
DEMO_CHECKSUM = hashlib.sha256(DEMO_TEXT.encode()).hexdigest()
DEMO_ACTIVE_FROM = datetime(2026, 1, 1, tzinfo=UTC)


def demo_bundle() -> PrivacyBundle:
    """Explicit synthetic proposals, never production approval/defaults."""
    policy = "SYNTHETIC DEMO/TEST POLICY ONLY. Do not enter real personal data."
    return PrivacyBundle.model_validate(
        {
            "approval_status": "SYNTHETIC ONLY",
            "approval_ref": "synthetic-demo-only",
            "approved_at": DEMO_ACTIVE_FROM,
            "consent": {
                "version": DEMO_VERSION,
                "purpose": DEMO_PURPOSE,
                "text": DEMO_TEXT,
                "checksum": DEMO_CHECKSUM,
            },
            "policy": {
                "version": "SYNTHETIC-POLICY-v1",
                "text": policy,
                "checksum": digest(policy),
                "withdrawal_instructions": (
                    "Synthetic only. No real requests or identity documents. "
                    "contact the demo operator through the agreed test channel."
                ),
            },
            "retention": {
                "chat_access_hours": 24,
                "chat_storage_days": 30,
                "order_pii_after_terminal_days": 180,
                "linked_snapshots_after_terminal_days": 180,
                "commercial_snapshots_after_terminal_days": 365,
                "consent_evidence": "until_order_deleted",
                "consent_text": "immutable_permanent",
                "audit_days": 90,
                "technical_logs_days": 14,
                "outbox_sent_days": 30,
                "outbox_failed_days": 30,
                "outbox_pending": "manual_review_no_ttl",
                "outbox_pending_review_hours": 24,
                "backup_days": 14,
                "deletion_journal_days": 14,
                "expert_review_after_content_retired_days": 365,
                "publication_evidence_after_content_retired_days": 365,
                "open_order_review_days": 180,
                "open_orders": "manual_review_no_ttl",
            },
        }
    )


def seed_consent(database: Database, *, mode: str) -> None:
    validate_environment(mode)
    with database.unit_of_work() as session:
        marker = session.scalar(
            text(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                "WHERE datname = current_database()"
            )
        )
        if marker != f"akbrizcase:synthetic-seed:{mode}":
            raise SeedRefused("Database is not explicitly marked for this seed mode")
        try:
            bundle = demo_bundle()
        except ValueError:
            raise SeedRefused("Conflicting synthetic consent fixture") from None
        session.execute(
            text("SELECT app.seed_demo_consent(CAST(:packet AS jsonb), :checksum)"),
            {
                "packet": json.dumps(bundle.model_dump(mode="json")),
                "checksum": bundle.checksum(),
            },
        )
        publication = session.get(ConsentPublication, DEMO_VERSION)
        if publication is None or publication.bundle_checksum != bundle.checksum():
            raise SeedRefused("Conflicting synthetic publication")
        row = session.scalars(
            select(ConsentText).where(ConsentText.version == DEMO_VERSION)
        ).one()
        if (row.purpose, row.text, row.checksum, row.active_from) != (
            DEMO_PURPOSE,
            DEMO_TEXT,
            DEMO_CHECKSUM,
            DEMO_ACTIVE_FROM,
        ):
            raise SeedRefused(
                "Conflicting synthetic consent version; operator review required"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("demo", "test"))
    args = parser.parse_args()
    database = None
    try:
        validate_environment(args.mode)
        database = Database(DatabaseSettings.from_environment())
        seed_consent(database, mode=args.mode)
    except (SeedRefused, DatabaseConfigurationError) as error:
        sys.exit(str(error))
    except SQLAlchemyError:
        sys.exit("Consent fixture failed; check isolated demo/test configuration")
    finally:
        if database is not None:
            database.close()
    print("Synthetic demo-only consent fixture ready; NOT legally approved")


if __name__ == "__main__":
    main()
