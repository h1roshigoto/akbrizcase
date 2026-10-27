"""Password-authenticated operator CLI; print technical outcomes after commit."""

import json
import sys
from getpass import GetPassWarning
from pathlib import Path
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.core.cli import SafeParser, hidden
from app.core.db import Database, DatabaseConfigurationError, DatabaseSettings
from app.modules.catalog.applicability_write import (
    Operation as ApplicabilityOperation,
)
from app.modules.catalog.applicability_write import run_applicability
from app.modules.kb.files import FilePipeline
from app.modules.kb.import_service import run_preparation
from app.modules.kb.lifecycle_service import Operation, run_lifecycle
from app.modules.kb.tree_service import run_tree


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
    tree_import = commands.add_parser("tree-import")
    tree_import.add_argument("--file", type=Path, required=True)
    tree_import.add_argument("--git-commit", required=True)
    tree_check = commands.add_parser("tree-check")
    selector = tree_check.add_mutually_exclusive_group(required=True)
    selector.add_argument("--file", type=Path)
    selector.add_argument("--tree", type=UUID)
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
    applicability: dict[str, ApplicabilityOperation] = {
        "applicability-" + name: name
        for name in ("candidate", "replace", "review", "publish", "revoke", "preview")
    }
    for name, action in applicability.items():
        command = commands.add_parser(name)
        command.add_argument("--item", type=UUID, required=True)
        command.add_argument("--model", type=UUID, required=True)
        if action in {"candidate", "replace"}:
            command.add_argument("--section", type=UUID, required=True)
        if action in {"review", "publish"}:
            command.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    database = None
    try:
        login = hidden("Acting admin login: ")
        password = hidden("Acting admin password: ")
        database = Database(DatabaseSettings.from_environment())
        if not database.ready():
            print("DATABASE_UNAVAILABLE", file=sys.stderr)
            return 1
        if args.command in {"tree-import", "tree-check"}:
            with database.unit_of_work() as session:
                tree_report = run_tree(
                    session,
                    login=login,
                    password=password,
                    operation="import" if args.command == "tree-import" else "check",
                    path=getattr(args, "file", None),
                    tree_id=getattr(args, "tree", None),
                    git_commit=getattr(args, "git_commit", None),
                )
            print(json.dumps(tree_report.technical(), sort_keys=True))
            return 0 if tree_report.ok else 1
        if args.command in applicability:
            with database.unit_of_work() as session:
                applicability_report = run_applicability(
                    session,
                    login=login,
                    password=password,
                    operation=applicability[args.command],
                    item_id=args.item,
                    model_id=args.model,
                    section_id=getattr(args, "section", None),
                    evidence_path=getattr(args, "evidence", None),
                )
            print(
                json.dumps(
                    {
                        "code": applicability_report.code,
                        **applicability_report.technical,
                    },
                    sort_keys=True,
                )
            )
            return 0 if applicability_report.ok else 1
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
