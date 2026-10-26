"""Bounded polling with safe errors and graceful SIGTERM/SIGINT shutdown."""

import logging
import signal
import time
from types import FrameType

from app.core.db import Database, DatabaseSettings
from app.modules.notify.smtp import (
    SMTPConfigurationError,
    SMTPSettings,
    send_notification,
)
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
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.info("Worker started")
    database = None
    try:
        try:
            settings = SMTPSettings.from_environment()
        except SMTPConfigurationError:
            logging.error("SMTP_CONFIG_INVALID")
            wait_for_stop()
            return
        if settings is None:
            logging.info("SMTP_DISABLED")
            wait_for_stop()
            return
        while not stop:
            try:
                if database is None:
                    database = Database(DatabaseSettings.from_environment())
                tick(database, settings, send_notification)
            except Exception:
                # Never log exceptions, DSNs, transport responses or message data.
                logging.error("WORKER_TICK_FAILED")
            wait_for_stop(POLL_SECONDS)
    finally:
        if database is not None:
            database.close()
        logging.info("Worker stopped gracefully")


if __name__ == "__main__":
    main()
