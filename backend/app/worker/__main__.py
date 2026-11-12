"""Bounded polling with safe errors and graceful SIGTERM/SIGINT shutdown."""

import signal
import time
from types import FrameType

from app.core.db import Database, DatabaseSettings
from app.core.observability import configure_logging, emit
from app.core.release import release_modes
from app.modules.notify.smtp import (
    SMTPConfigurationError,
    SMTPSettings,
    send_notification,
)
from app.worker.cleanup import cleanup_rate_limits, cleanup_tick
from app.worker.runner import tick

POLL_SECONDS = 2


def main() -> None:
    stop = False

    def request_stop(signum: int, frame: FrameType | None) -> None:
        # A signal can interrupt Event.wait while its non-reentrant lock is held.
        # Only assign here: taking that lock again in Event.set would deadlock.
        nonlocal stop
        stop = True

    def wait_for_stop(timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not stop:
            delay = 0.1
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                delay = min(delay, remaining)
            # Bound shutdown latency even if the signal arrives just before
            # sleep; Python may restart an interrupted sleep after the handler.
            time.sleep(delay)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    configure_logging()
    try:
        modes = release_modes()  # Unsupported transport must stop startup safely.
    except ValueError:
        raise SystemExit("RELEASE_CONFIG_INVALID") from None
    emit("WORKER_STARTED")
    database = None
    try:
        try:
            settings = SMTPSettings.from_environment()
        except SMTPConfigurationError:
            emit("SMTP_CONFIG_INVALID")
            if modes is not None and modes.smtp != "disabled":
                raise SystemExit("SMTP_CONFIG_INVALID") from None
            settings = None
        else:
            if settings is None:
                if modes is not None and modes.smtp != "disabled":
                    raise SystemExit("SMTP_CONFIG_INVALID")
                emit("SMTP_DISABLED")
        while not stop:
            try:
                if database is None:
                    database = Database(DatabaseSettings.from_environment())
                # Expired IP aliases are removed independently of privacy release gates.
                cleanup_rate_limits(database)
                # Cleanup has its own error boundary and runs with SMTP off.
                cleanup_tick(database, stopped=lambda: stop)
                tick(database, settings, send_notification)
            except Exception:
                # Never log exceptions, DSNs, transport responses or message data.
                emit("WORKER_TICK_FAILED")
            wait_for_stop(POLL_SECONDS)
    finally:
        if database is not None:
            database.close()
        emit("WORKER_STOPPED")


if __name__ == "__main__":
    main()
