"""Password-authenticated operator CLI; print technical outcomes after commit."""

import json
import sys
from getpass import GetPassWarning
from pathlib import Path
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.core.cli import SafeParser, hidden
from app.core.db import Database, DatabaseConfigurationError, DatabaseSettings
from app.modules.kb.files import FilePipeline
from app.modules.kb.import_service import run_preparation
from app.modules.kb.lifecycle_service import Operation, run_lifecycle


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description="Authenticated KB preparation and lifecycle")
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=SafeParser
    )
    for name in ("kb-import", "kb-check"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        if name == "kb-import":
            command.add_argument("--file", type=Path, required=True)
    operations: dict[str, Operation] = {
        "kb-" + name: name
        for name in ("review", "publish", "retire", "revoke", "visibility", "preview")
    }
    for name, operation in operations.items():
        command = commands.add_parser(name)
        command.add_argument("--document", type=UUID, required=True)
        if operation != "visibility":
            command.add_argument(
                "--version",
                type=UUID,
                required=operation in {"review", "publish", "retire"},
            )
        if operation in {"review", "visibility"}:
            command.add_argument(
                "--evidence", type=Path, required=operation == "review"
            )
        if operation == "visibility":
            command.add_argument(
                "--visibility", choices=("public", "internal"), required=True
            )
    args = parser.parse_args(argv)
    database = None
    try:
        login = hidden("Acting admin login: ")
        password = hidden("Acting admin password: ")
        database = Database(DatabaseSettings.from_environment())
        if not database.ready():
            print("DATABASE_UNAVAILABLE", file=sys.stderr)
            return 1
        if args.command in operations:
            with database.unit_of_work() as session:
                lifecycle = run_lifecycle(
                    session,
                    login=login,
                    password=password,
                    operation=operations[args.command],
                    document_id=args.document,
                    version_id=getattr(args, "version", None),
                    evidence_path=getattr(args, "evidence", None),
                    visibility=getattr(args, "visibility", None),
                    pipeline=FilePipeline.from_environment(),
                )
            # The outer commit has succeeded; failures never print a success.
            print(
                json.dumps(
                    {"code": lifecycle.code, **lifecycle.technical}, sort_keys=True
                )
            )
            return 0 if lifecycle.ok else 1
        with database.unit_of_work() as session:
            report = run_preparation(
                session,
                login=login,
                password=password,
                manifest_path=args.manifest,
                source_path=args.file if args.command == "kb-import" else None,
                pipeline=FilePipeline.from_environment(),
            )
        if report.code == "PREPARATION_INCOMPLETE":
            # A separate explicit transaction retains an empty draft. Recheck
            # credentials, active role, source, manifest and AV, including the
            # final prepare() checks before this transaction's commit.
            with database.unit_of_work() as session:
                report = run_preparation(
                    session,
                    login=login,
                    password=password,
                    manifest_path=args.manifest,
                    source_path=args.file,
                    pipeline=FilePipeline.from_environment(),
                    retain_incomplete=True,
                )
        reason = f" reason={report.reason}" if report.reason else ""
        print(f"{report.code} sections={report.section_count}{reason}")
        return 0 if report.ok else 1
    except (GetPassWarning, EOFError, KeyboardInterrupt):
        print("SECURE_INPUT_UNAVAILABLE", file=sys.stderr)
        return 1
    except (DatabaseConfigurationError, SQLAlchemyError, OSError, ValueError):
        print("OPERATION_FAILED_ROLLED_BACK", file=sys.stderr)
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    sys.exit(main())
