"""Protected staff CLI. Secrets enter only through a non-echoing terminal."""

import argparse
import sys
import warnings
from getpass import GetPassWarning, getpass
from typing import NoReturn
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.core.db import Database, DatabaseConfigurationError, DatabaseSettings
from app.core.staff import bootstrap_admin, create_staff, disable_staff


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's default error can echo unrecognized secret-bearing arguments.
        self.exit(2, "Invalid command arguments; use --help.\n")


def hidden(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        return getpass(prompt)


def new_password() -> str:
    password = hidden("New individual password (12+ characters): ")
    if password != hidden("Confirm new password: "):
        raise ValueError("Password confirmation failed")
    return password


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description="Staff operations in a protected operator terminal")
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=SafeParser
    )
    commands.add_parser("staff-bootstrap", help="Create the first admin once")
    create = commands.add_parser(
        "staff-create", help="Authenticate admin and create staff"
    )
    create.add_argument("--role", choices=("manager", "admin"), required=True)
    disable = commands.add_parser(
        "staff-disable", help="Authenticate admin and disable staff"
    )
    disable.add_argument("--user-id", type=UUID, required=True)
    args = parser.parse_args(argv)
    database = None
    try:
        if args.command == "staff-bootstrap":
            login = hidden("First admin login: ")
            password = new_password()
        else:
            admin_login = hidden("Acting admin login: ")
            admin_password = hidden("Acting admin password: ")
            if args.command == "staff-create":
                login = hidden("New staff login: ")
                password = new_password()
        database = Database(DatabaseSettings.from_environment())
        if not database.ready():
            print("Database unavailable or schema/role incompatible.", file=sys.stderr)
            return 1
        with database.unit_of_work() as session:
            if args.command == "staff-bootstrap":
                outcome = bootstrap_admin(session, login=login, password=password)
            elif args.command == "staff-create":
                outcome = create_staff(
                    session,
                    admin_login=admin_login,
                    admin_password=admin_password,
                    login=login,
                    password=password,
                    role=args.role,
                )
            else:
                outcome = disable_staff(
                    session,
                    admin_login=admin_login,
                    admin_password=admin_password,
                    user_id=args.user_id,
                )
        # Print only after the outer UoW has committed, including denied audits.
        print(outcome.result + (f" {outcome.user_id}" if outcome.ok else ""))
        return 0 if outcome.ok else 1
    except (GetPassWarning, EOFError, KeyboardInterrupt, ValueError):
        print(
            "Secure input unavailable or invalid; operation cancelled.", file=sys.stderr
        )
        return 1
    except (DatabaseConfigurationError, SQLAlchemyError, OSError):
        print("Staff operation unavailable; transaction rolled back.", file=sys.stderr)
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    sys.exit(main())
