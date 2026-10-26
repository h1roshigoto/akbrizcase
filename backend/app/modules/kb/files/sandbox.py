"""Fixed trusted tasks operating only on immutable anonymous file descriptors."""

import fcntl
import os
import sys
from pathlib import Path

from app.modules.kb.files.process import (
    DEFAULT_LIMITS,
    FileRejected,
    Limits,
    run_bounded,
)

WORKER = str(Path(__file__).with_name("worker.py"))


def sealed_snapshot(data: bytes) -> int:
    descriptor = os.memfd_create("document", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        with os.fdopen(os.dup(descriptor), "wb") as file:
            file.write(data)
            file.flush()
        # Linux UAPI constants (some CPython builds omit their Python names).
        fcntl.fcntl(descriptor, 1033, 0x000F)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def run_parser(
    descriptor: int, task: str = "inspect", *, limits: Limits = DEFAULT_LIMITS
) -> bytes:
    if task not in {
        "probe",
        "pdf",
        "pdf-info",
        "pdf-text",
        "inspect",
        "network",
        "filesystem",
        "write",
        "fork",
        "cpu",
        "memory",
        "timeout",
        "output",
    }:
        raise FileRejected("PARSER_TASK")
    os.lseek(descriptor, 0, os.SEEK_SET)
    code, output = run_bounded(
        [
            sys.executable,
            "-I",
            WORKER,
            task,
            str(descriptor),
            str(limits.cpu),
            str(limits.memory),
        ],
        fds=(descriptor,),
        limits=limits,
    )
    if code != 0:
        raise FileRejected("PARSER_REJECTED")
    return output


def run_tool(command: list[str], *, fds: tuple[int, ...], limits: Limits) -> bytes:
    code, output = run_bounded(
        [
            sys.executable,
            "-I",
            WORKER,
            "exec",
            "-1",
            str(limits.cpu),
            str(limits.memory),
            *command,
        ],
        fds=fds,
        limits=limits,
    )
    if code == 1 and command[0] == "/usr/bin/clamscan":
        raise FileRejected("SCANNER_INFECTED")
    if code != 0:
        raise FileRejected("SCANNER_FAILURE")
    return output
