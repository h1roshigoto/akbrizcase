"""Protected publication CLI. No public edit API and no credentials in argv."""

import sys
from getpass import GetPassWarning
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.cli import SafeParser, hidden
from app.core.db import SCHEMA_REVISION, Database, DatabaseSettings
from app.core.db_types import utc_now
from app.core.privacy import load_bundle
from app.modules.orders.privacy_operator import run_privacy


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    parser.add_argument("operation", choices=("check", "publish", "retire"))
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--expected-version")
    selector.add_argument("--expect-empty", action="store_true")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument(
        "--checksum", help="Independently reviewed canonical bundle SHA256"
    )
    args = parser.parse_args(argv)
    if args.operation != "check" and not (args.expected_version or args.expect_empty):
        parser.error("Expected current version required")
    database = None
    try:
        if args.operation == "check":
            if args.bundle is None:
                raise ValueError
            bundle = load_bundle(args.bundle)
            if not bundle.valid_at(utc_now()):
                raise ValueError
            print("VALID_PACKET " + bundle.checksum())
            return 0
        login = hidden("Acting admin login: ")
        password = hidden("Acting admin password: ")
        database = Database(DatabaseSettings.from_environment(migration=True))
        with database.unit_of_work() as session:
            if set(
                session.scalars(text("SELECT version_num FROM app.alembic_version"))
            ) != {SCHEMA_REVISION}:
                raise ValueError
            outcome = run_privacy(
                session,
                login=login,
                password=password,
                operation=args.operation,
                expected_version=args.expected_version,
                path=args.bundle,
                expected_checksum=args.checksum,
            )
        print(outcome.code)
        return 0 if outcome.ok else 1
    except (
        GetPassWarning,
        EOFError,
        KeyboardInterrupt,
        ValueError,
        OSError,
        SQLAlchemyError,
    ):
        print(
            "Privacy operation unavailable; transaction rolled back.", file=sys.stderr
        )
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    sys.exit(main())
