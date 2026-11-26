"""One-shot runtime-role readiness; never migrates or prints driver failures."""

import smtplib
import sys

from app.core.csrf import StaffSecurity
from app.core.db import Database, DatabaseSettings
from app.core.rate_limits import IpSecurity
from app.core.release import FullReleaseModes, release_modes
from app.modules.kb.files.scanner import ClamAV
from app.modules.notify.smtp import SMTPSettings
from app.modules.orders.security import OrderSecurity


def dependencies_failure_reason() -> str | None:
    try:
        modes = release_modes()
    except ValueError:
        return "RELEASE_MODE_INVALID"
    if not isinstance(modes, FullReleaseModes):
        return None
    try:
        ClamAV()._database()  # Signed, fresh, immutable CVD in operator usage.
    except Exception:
        return "AV_UNAVAILABLE"
    try:
        settings = SMTPSettings.from_environment()
        if settings is None or settings.mode != "staging-sink":
            return "SMTP_UNAVAILABLE"
        client = smtplib.SMTP(timeout=2)
        try:
            client.connect(settings.host, settings.port)
            if client.ehlo()[0] != 250:
                return "SMTP_UNAVAILABLE"
        finally:
            client.close()
    except Exception:
        return "SMTP_UNAVAILABLE"
    return None


def dependencies_ready() -> bool:
    return dependencies_failure_reason() is None


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
        return 0 if database.ready() and dependencies_ready() else 1
    except Exception:
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    sys.exit(main())
