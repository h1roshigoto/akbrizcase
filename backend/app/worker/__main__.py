"""Bounded polling with safe errors and graceful SIGTERM/SIGINT shutdown."""

import logging
import signal
from threading import Event
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
    stop = Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

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
            stop.wait()
            return
        if settings is None:
            logging.info("SMTP_DISABLED")
            stop.wait()
            return
        while not stop.is_set():
            try:
                if database is None:
                    database = Database(DatabaseSettings.from_environment())
                tick(database, settings, send_notification)
            except Exception:
                # Never log exceptions, DSNs, transport responses or message data.
                logging.error("WORKER_TICK_FAILED")
            stop.wait(POLL_SECONDS)
    finally:
        if database is not None:
            database.close()
        logging.info("Worker stopped gracefully")


if __name__ == "__main__":
    main()
