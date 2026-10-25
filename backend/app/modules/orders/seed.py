"""Explicit synthetic consent fixture. This is NOT legally approved language."""

import argparse
import hashlib
import sys
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.core.db import Database, DatabaseConfigurationError, DatabaseSettings
from app.modules.catalog.seed import SeedRefused, validate_environment
from app.modules.orders.models import ConsentText

DEMO_VERSION = "SYNTHETIC-DEMO-ONLY-v1"
DEMO_PURPOSE = "SYNTHETIC DEMO/TEST ONLY — NOT LEGALLY APPROVED"
DEMO_TEXT = (
    "SYNTHETIC DEMO/TEST CONSENT PLACEHOLDER. "
    "Not legally approved. Not for collecting real personal data."
)
DEMO_CHECKSUM = hashlib.sha256(DEMO_TEXT.encode()).hexdigest()
DEMO_ACTIVE_FROM = datetime(2026, 1, 1, tzinfo=UTC)


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
        session.execute(
            insert(ConsentText)
            .values(
                version=DEMO_VERSION,
                purpose=DEMO_PURPOSE,
                text=DEMO_TEXT,
                checksum=DEMO_CHECKSUM,
                active_from=DEMO_ACTIVE_FROM,
            )
            .on_conflict_do_nothing(index_elements=[ConsentText.version])
        )
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
