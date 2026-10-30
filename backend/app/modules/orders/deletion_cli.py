"""Protected deletion after verified identity and a responsible owner's decision."""

import os
import sys
from getpass import GetPassWarning
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.cli import SafeParser, hidden
from app.core.db import SCHEMA_REVISION, Database, DatabaseSettings
from app.modules.orders.cleanup_service import apply_command
from app.modules.orders.deletion_journal import SyntheticJournal
from app.modules.orders.deletion_operator import authorize, preflight, prepare_deletion
from app.modules.orders.models import Order
from app.modules.orders.privacy_service import synthetic_database


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    parser.add_argument("order_id", type=UUID)
    parser.add_argument(
        "--journal-policy-version",
        required=True,
        help="Explicit approved publication governing journal retention only",
    )
    parser.add_argument(
        "--decision-id",
        type=UUID,
        required=True,
        help="Nonpersonal UUID in the protected decision registry",
    )
    args = parser.parse_args(argv)
    database = None
    try:
        login, password = (
            hidden("Acting admin login: "),
            hidden("Acting admin password: "),
        )
        database = Database(DatabaseSettings.from_environment(migration=True))
        with database.unit_of_work() as session:
            if set(
                session.scalars(text("SELECT version_num FROM app.alembic_version"))
            ) != {SCHEMA_REVISION}:
                raise ValueError
            code, version = preflight(
                session, login=login, password=password, order_id=args.order_id
            )
            synthetic = synthetic_database(session)
        if code != "READY" or version is None:
            print(code)
            return 0 if code == "ABSENT" else 1
        directory = os.environ.get("SYNTHETIC_DELETION_JOURNAL_DIR")
        if not synthetic or not directory:
            print("INDEPENDENT_JOURNAL_REQUIRED")
            return 1
        confirmation = hidden(
            "After identity verification and approval, enter DELETE "
            "followed by target UUID: "
        )
        if confirmation != f"DELETE {args.order_id}":
            print("CANCELLED")
            return 1
        with database.unit_of_work() as session:
            code, command = prepare_deletion(
                session,
                login=login,
                password=password,
                order_id=args.order_id,
                expected_version=version,
                decision_id=args.decision_id,
                journal_policy_version=args.journal_policy_version,
            )
        if command is None:
            print(code)
            return 0 if code == "ABSENT" else 1
        # Immutable authorization is installed before deletion, outside the UoW.
        witness = SyntheticJournal(Path(directory)).witness(
            command, synthetic=synthetic
        )
        with database.unit_of_work() as session:
            code = authorize(session, login, password)
            if code != "SUCCESS":
                print(code)
                return 1
            row = session.scalar(
                select(Order).where(Order.id == args.order_id).with_for_update()
            )
            if row is not None and row.version != version:
                print("STATE_CONFLICT")
                return 1
            changed = apply_command(session, command, witness, manual_authorized=True)
            pending = (
                session.scalar(select(Order.id).where(Order.id == args.order_id))
                is not None
            )
        print("PENDING" if pending else "APPLIED" if changed else "ABSENT")
        return 1 if pending else 0
    except (
        GetPassWarning,
        EOFError,
        KeyboardInterrupt,
        ValueError,
        OSError,
        SQLAlchemyError,
    ):
        # A commit acknowledgement can be lost after the server committed. Do
        # not falsely claim rollback; the persisted command makes retry safe.
        print(
            "Deletion outcome unconfirmed; retry the same target and decision.",
            file=sys.stderr,
        )
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    sys.exit(main())
