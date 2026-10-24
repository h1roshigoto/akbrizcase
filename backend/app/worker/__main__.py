"""Run an idle worker until SIGTERM or SIGINT requests a graceful exit."""

import logging
import signal
from threading import Event
from types import FrameType


def main() -> None:
    stop = Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.info("Worker started (idle skeleton)")
    stop.wait()
    logging.info("Worker stopped gracefully")


if __name__ == "__main__":
    main()
