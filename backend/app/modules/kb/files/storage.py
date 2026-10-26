"""Directory-fd anchored private storage; never follow client paths or symlinks."""

import os
import re
import stat
from pathlib import Path

from app.modules.kb.files.process import FileRejected

MAX_BYTES = 25 * 1024 * 1024
KEY = re.compile(r"[a-f0-9]{64}\Z")
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def valid_key(key: str) -> None:
    if not KEY.fullmatch(key):
        raise FileRejected("FILE_KEY")


def open_root(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise FileRejected("STORAGE_CONFIG")
    descriptor = os.open("/", DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise FileRejected("STORAGE_PERMISSIONS")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_area(root: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=root)
    except FileExistsError:
        pass
    descriptor = os.open(name, DIRECTORY, dir_fd=root)
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        os.close(descriptor)
        raise FileRejected("STORAGE_PERMISSIONS")
    return descriptor


def create(directory: int, key: str, data: bytes) -> None:
    valid_key(key)
    descriptor = os.open(
        key,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory,
    )
    with os.fdopen(descriptor, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
        os.fchmod(file.fileno(), 0o400)
    os.fsync(directory)


def read(directory: int, key: str, maximum: int = MAX_BYTES) -> bytes:
    valid_key(key)
    descriptor = os.open(
        key,
        os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=directory,
    )
    with os.fdopen(descriptor, "rb") as file:
        metadata = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_size > maximum
            or stat.S_IMODE(metadata.st_mode) != 0o400
        ):
            raise FileRejected("STORAGE_FILE")
        data = file.read(maximum + 1)
        if not data or len(data) > maximum:
            raise FileRejected("FILE_SIZE")
        return data
