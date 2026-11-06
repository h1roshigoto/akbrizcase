"""One-shot migration entrypoint. Never called by API or worker startup."""

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError


def failure_code(error: Exception) -> str:
    """Closed diagnostic vocabulary; never format SQL/driver errors or paths."""
    if isinstance(error, DBAPIError):
        state = getattr(error.orig, "sqlstate", None)
        if not isinstance(state, str):
            state = ""
        return {
            "28P01": "MIGRATION_AUTH_FAILED",
            "42501": "MIGRATION_PRIVILEGE_FAILED",
            "42P01": "MIGRATION_SCHEMA_FAILED",
            "3F000": "MIGRATION_SCHEMA_FAILED",
            "23514": "MIGRATION_CONSTRAINT_FAILED",
        }.get(state, "MIGRATION_DATABASE_FAILED")
    if isinstance(error, OSError):
        return "MIGRATION_FILE_FAILED"
    return "MIGRATION_CONFIGURATION_FAILED"


def main() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    try:
        command.upgrade(config, "head")
    except Exception as error:
        # Avoid printing driver exceptions, SQL, DSNs or secret file contents.
        sys.exit(failure_code(error))
    print("Schema migrated to head")


if __name__ == "__main__":
    main()
