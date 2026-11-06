"""One-shot runtime-role readiness; never migrates or prints driver failures."""

import sys

from app.core.csrf import StaffSecurity
from app.core.db import Database, DatabaseSettings
from app.core.rate_limits import IpSecurity
from app.core.release import release_modes
from app.modules.orders.security import OrderSecurity


def main() -> int:
    database = None
    try:
        if release_modes() is None:
            return 1
        if "--worker" not in sys.argv and any(
            value is None
            for value in (
                StaffSecurity.from_environment(),
                IpSecurity.from_environment(),
                OrderSecurity.from_environment(),
            )
        ):
            return 1
        database = Database(DatabaseSettings.from_environment())
        return 0 if database.ready() else 1
    except Exception:
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    sys.exit(main())
