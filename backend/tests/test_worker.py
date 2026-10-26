"""Check the real worker process exits on both supported stop signals."""

import os
import signal
import subprocess
import sys

import pytest


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("invalid", [False, True])
def test_worker_stops_gracefully(stop_signal: signal.Signals, invalid: bool) -> None:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("SMTP_")
    }
    if invalid:
        environment["SMTP_HOST"] = "private-invalid-config"
    process = subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
    )
    assert process.stdout is not None
    try:
        assert process.stdout.readline().strip() == "Worker started"
        process.send_signal(stop_signal)
        output, _ = process.communicate(timeout=5)
        assert process.returncode == 0
        assert "Worker stopped gracefully" in output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
