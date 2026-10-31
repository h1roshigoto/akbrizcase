"""Protected deletion after verified identity and a responsible owner's decision."""

import os
import sys
from getpass import GetPassWarning
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.cli import SafeParser, hidden
from app.core.db import SCHEMA_REVISION, Database, DatabaseSettings
from app.modules.orders.deletion_journal import SyntheticJournal, command_for
from app.modules.orders.deletion_models import DeletionEntry
from app.modules.orders.deletion_operator import authorize, preflight, prepare_deletion
from app.modules.orders.deletion_reconciliation import JournalState, reconcile_command
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
            previous = None
            if code == "ABSENT":
                entry = session.scalar(
                    select(DeletionEntry).where(
                        DeletionEntry.operation == "order_delete",
                        DeletionEntry.object_id == args.order_id,
                        DeletionEntry.decision_id == args.decision_id,
                    )
                )
                if entry is not None and (
                    command_for(entry).journal_version == args.journal_policy_version
                ):
                    previous = command_for(entry)
        if code != "READY" and previous is None:
            print(code)
            return 0 if code == "ABSENT" else 1
        directory = os.environ.get("SYNTHETIC_DELETION_JOURNAL_DIR")
        if not synthetic or not directory:
            print("INDEPENDENT_JOURNAL_REQUIRED")
            return 1
        command = previous
        if command is None:
            confirmation = hidden(
                "After identity verification and approval, enter DELETE "
                "followed by target UUID: "
            )
            if confirmation != f"DELETE {args.order_id}":
                print("CANCELLED")
                return 1
            assert version is not None
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

        def reauthorize(session: Session) -> bool:
            code = authorize(session, login, password)
            if code != "SUCCESS":
                return False
            row = session.scalar(
                select(Order).where(Order.id == args.order_id).with_for_update()
            )
            return row is None or row.version == version

        result = reconcile_command(
            database,
            command,
            SyntheticJournal(Path(directory)),
            execute=True,
            authorize_manual=reauthorize,
        )
        print(result.state)
        return (
            0 if result.state == JournalState.APPLIED and result.commit_confirmed else 1
        )
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
