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


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize(
    "boundary",
    [
        "config-disabled",
        "config-invalid",
        "wait-disabled",
        "wait-invalid",
        "wait-polling",
        "tick-success",
        "tick-error",
    ],
)
def test_worker_shutdown_boundaries(stop_signal: signal.Signals, boundary: str) -> None:
    # Keep real signal handlers in an isolated process. Inject delivery at the
    # startup/wait boundary instead of relying on scheduler timing or sleeps.
    script = """
import signal
import sys
from unittest.mock import Mock, patch

from app.modules.notify.smtp import SMTPConfigurationError
from app.worker.__main__ import main

boundary = sys.argv[1]
stop_signal = int(sys.argv[2])

def request_stop(*args, **kwargs):
    signal.raise_signal(stop_signal)
    signal.raise_signal(stop_signal)  # Repeated stop requests are harmless.

def configuration():
    if boundary.startswith('config-'):
        request_stop()
    if boundary.endswith('invalid'):
        raise SMTPConfigurationError()
    return None if boundary.endswith('disabled') else object()

completed = []
def tick(*args):
    if boundary.startswith('tick-'):
        request_stop()
        completed.append('tick completed')
        if boundary == 'tick-error':
            raise RuntimeError('private tick error')
    return False

database = Mock()
with (
    patch('app.worker.__main__.SMTPSettings.from_environment', configuration),
    patch('app.worker.__main__.DatabaseSettings.from_environment'),
    patch('app.worker.__main__.Database', return_value=database) as factory,
    patch('app.worker.__main__.tick', side_effect=tick) as poll,
    patch('time.sleep', side_effect=request_stop),
    # Event.wait calls Condition.wait with its non-reentrant lock held. This
    # delivers a real signal in the old deadlock window deterministically.
    patch('threading.Condition.wait', side_effect=request_stop),
):
    main()
    if boundary.startswith('tick-') or boundary == 'wait-polling':
        factory.assert_called_once()
        poll.assert_called_once()
        database.close.assert_called_once()
        assert completed == ([] if boundary == 'wait-polling' else ['tick completed'])
    else:
        factory.assert_not_called()
        poll.assert_not_called()
        database.close.assert_not_called()
"""
    process = subprocess.run(
        [sys.executable, "-c", script, boundary, str(int(stop_signal))],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert process.stderr.count("Worker stopped gracefully") == 1
    assert ("SMTP_CONFIG_INVALID" in process.stderr) == boundary.endswith("invalid")
    assert ("SMTP_DISABLED" in process.stderr) == boundary.endswith("disabled")
    assert ("WORKER_TICK_FAILED" in process.stderr) == (boundary == "tick-error")
    assert "private tick error" not in process.stderr
