"""Cross-process bounded admission for heavy file scan/decode pipelines."""

import fcntl
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.modules.kb.files import storage
from app.modules.kb.files.process import FileRejected


class FileAdmission:
    """Bounds concurrent heavy AV/parser executions across API worker processes."""

    def __init__(
        self,
        root: Path,
        *,
        max_concurrent: int = 2,
        wait_timeout: float = 20.0,
        poll_interval: float = 0.025,
    ) -> None:
        if not 1 <= max_concurrent <= 4:
            raise ValueError("max_concurrent must be between 1 and 4")
        if not 0 < wait_timeout <= 30.0:
            raise ValueError("wait_timeout must be between 0 and 30 seconds")
        if not 0 < poll_interval <= 1.0:
            raise ValueError("poll_interval must be between 0 and 1 second")
        self.root = root
        self.max_concurrent = max_concurrent
        self.wait_timeout = wait_timeout
        self.poll_interval = poll_interval

    def _open_slot(self, locks_fd: int, slot: int) -> int:
        try:
            descriptor = os.open(
                f"slot-{slot}.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=locks_fd,
            )
        except OSError:
            raise FileRejected("STORAGE_FAILURE") from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise FileRejected("STORAGE_FILE")
            return descriptor
        except OSError:
            os.close(descriptor)
            raise FileRejected("STORAGE_FAILURE") from None
        except BaseException:
            os.close(descriptor)
            raise

    @contextmanager
    def acquire(self) -> Iterator[None]:
        """Acquire one cross-process slot within wait_timeout or fail closed."""
        try:
            root_fd = storage.open_root(self.root)
            try:
                locks_fd = storage.open_area(root_fd, "locks")
            finally:
                os.close(root_fd)
        except OSError:
            raise FileRejected("STORAGE_FAILURE") from None

        slot_fds: list[int] = []
        try:
            for idx in range(self.max_concurrent):
                slot_fds.append(self._open_slot(locks_fd, idx))
        except BaseException:
            for fd in slot_fds:
                os.close(fd)
            os.close(locks_fd)
            raise
        os.close(locks_fd)

        acquired_fd: int | None = None
        deadline = time.monotonic() + self.wait_timeout
        try:
            while acquired_fd is None:
                for fd in slot_fds:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired_fd = fd
                        break
                    except BlockingIOError:
                        continue
                    except OSError:
                        raise FileRejected("STORAGE_FAILURE") from None
                if acquired_fd is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FileRejected("PIPELINE_BUSY")
                time.sleep(min(self.poll_interval, remaining))
        finally:
            for fd in slot_fds:
                if fd != acquired_fd:
                    os.close(fd)

        try:
            yield
        finally:
            try:
                fcntl.flock(acquired_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(acquired_fd)
