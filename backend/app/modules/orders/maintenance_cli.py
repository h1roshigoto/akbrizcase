"""One-shot protected synthetic maintenance. No unattended scheduling mode."""

import json
import os
import signal
import sys
from getpass import GetPassWarning
from pathlib import Path
from threading import Event

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.cli import SafeParser, hidden
from app.core.db import SCHEMA_REVISION, Database, DatabaseSettings
from app.modules.orders.audit_maintenance import (
    audit_maintenance_tick,
    authorize_maintenance,
)
from app.modules.orders.deletion_journal import SyntheticJournal
from app.modules.orders.retention_review import aggregate_retention_report


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    parser.add_argument("operation", choices=("audit", "review"))
    parser.add_argument("--limit", type=int, choices=range(1, 101), default=100)
    parser.add_argument("--max-batches", type=int, choices=range(1, 11), default=10)
    args = parser.parse_args(argv)
    database = None
    stopped = Event()
    previous = {}
    try:
        login = hidden("Acting admin login: ")
        password = hidden("Acting admin password: ")
        database = Database(DatabaseSettings.from_environment(migration=True))
        with database.unit_of_work() as session:
            if args.operation == "review":
                session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            if set(
                session.scalars(text("SELECT version_num FROM app.alembic_version"))
            ) != {SCHEMA_REVISION}:
                raise ValueError
            code = authorize_maintenance(
                session,
                login,
                password,
                action="privacy.retention_review"
                if args.operation == "review"
                else "privacy.audit_maintenance",
            )
            report = (
                aggregate_retention_report(
                    session, limit=args.limit, max_batches=args.max_batches
                )
                if (code == "SUCCESS" and args.operation == "review")
                else None
            )
        if code != "SUCCESS":
            print(code)
            return 1
        if report is not None:
            print(json.dumps(report, sort_keys=True))
            return 0
        directory = os.environ.get("SYNTHETIC_DELETION_JOURNAL_DIR")
        if not directory:
            print("INDEPENDENT_JOURNAL_REQUIRED")
            return 1
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stopped.set())
        counts = audit_maintenance_tick(
            database,
            SyntheticJournal(Path(directory)),
            login=login,
            password=password,
            limit=args.limit,
            stopped=stopped.is_set,
        )
        print(json.dumps(counts, sort_keys=True))
        return (
            1
            if stopped.is_set()
            or any(
                counts.get(code)
                for code in (
                    "AUTHORITY_DENIED",
                    "manual_review",
                    "CONFLICT",
                    "REVIEW_REQUIRED",
                    "SINK_UNAVAILABLE",
                    "UNCONFIRMED",
                    "WITNESSED",
                    "PREPARED",
                    "LOCAL_MISSING",
                )
            )
            else 0
        )
    except (
        GetPassWarning,
        EOFError,
        KeyboardInterrupt,
        ValueError,
        OSError,
        SQLAlchemyError,
    ):
        print(
            "Maintenance outcome unconfirmed; protected review required.",
            file=sys.stderr,
        )
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if database is not None:
            database.close()


if __name__ == "__main__":
    sys.exit(main())
