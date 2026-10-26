"""Bounded local processes. Child output is never logged or included in errors."""

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass


class FileRejected(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Limits:
    cpu: int = 3
    memory: int = 256 * 1024 * 1024
    seconds: float = 5
    output: int = 16384


DEFAULT_LIMITS = Limits()


def run_bounded(
    command: list[str], *, fds: tuple[int, ...] = (), limits: Limits = DEFAULT_LIMITS
) -> tuple[int, bytes]:
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            cwd="/",
            close_fds=True,
            pass_fds=fds,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        output = bytearray()
        deadline = time.monotonic() + limits.seconds
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdout, process.stderr):
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FileRejected("PROCESS_TIMEOUT")
                for key, _ in selector.select(min(remaining, 0.1)):
                    data = os.read(key.fd, 4096)
                    if not data:
                        selector.unregister(key.fd)
                    else:
                        output.extend(data)
                        if len(output) > limits.output:
                            raise FileRejected("PROCESS_OUTPUT_LIMIT")
            try:
                code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise FileRejected("PROCESS_TIMEOUT") from None
        return code, bytes(output)
    except OSError:
        raise FileRejected("PROCESS_UNAVAILABLE") from None
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
