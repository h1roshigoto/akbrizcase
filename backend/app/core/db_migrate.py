"""One-shot migration entrypoint. Never called by API or worker startup."""

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config


def main() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    try:
        command.upgrade(config, "head")
    except Exception:
        # Avoid printing driver exceptions, SQL, DSNs or secret file contents.
        sys.exit("Migration failed; check DB availability, role and schema settings")
    print("Schema migrated to head")


if __name__ == "__main__":
    main()
