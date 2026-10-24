"""Check the real worker process exits on both supported stop signals."""

import signal
import subprocess
import sys

import pytest


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_worker_stops_gracefully(stop_signal: signal.Signals) -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    try:
        assert process.stdout.readline().strip() == "Worker started (idle skeleton)"
        process.send_signal(stop_signal)
        output, _ = process.communicate(timeout=5)
        assert process.returncode == 0
        assert "Worker stopped gracefully" in output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
