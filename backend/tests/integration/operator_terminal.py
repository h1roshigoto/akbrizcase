"""Bounded real terminal input for synthetic operator acceptance only."""

import os
import pty
import select
import subprocess
import sys
import time


def terminal(
    module: str, arguments: list[str], answers: list[tuple[bytes, str]]
) -> tuple[int, bytes]:
    master, slave = pty.openpty()
    child = subprocess.Popen(
        [sys.executable, "-m", module, *arguments],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output = b""
    pending = list(answers)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break  # PTY EOF is EIO on Linux.
                if not chunk:
                    break
                output += chunk
                assert len(output) <= 65536, "Operator output exceeded bound"
                if pending and pending[0][0] in output:
                    _, value = pending.pop(0)
                    os.write(master, (value + "\n").encode())
        code = child.wait(timeout=2)
        assert not pending, "Operator prompts were not completed"
        assert all(value.encode() not in output for _, value in answers), (
            "Hidden operator input was echoed"
        )
        return code, output
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        os.close(master)
