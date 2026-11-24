"""Cross-process rate and daily token budget enforcement for LLM calls."""

import fcntl
import json
import os
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.modules.kb.files import storage
from app.modules.kb.files.process import FileRejected
from app.modules.llm.schemas import LLMError


class BudgetLedger(Protocol):
    def reserve(self, estimated_tokens: int) -> None: ...

    def record_usage(self, actual_tokens: int, estimated_tokens: int) -> None: ...


def _utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class InMemoryBudgetLedger:
    """Thread-safe in-memory ledger for isolated unit tests."""

    def __init__(
        self, *, daily_token_budget: int, max_requests_per_minute: int
    ) -> None:
        self.daily_token_budget = daily_token_budget
        self.max_requests_per_minute = max_requests_per_minute
        self._lock = threading.Lock()
        self._day = _utc_day()
        self._tokens_used = 0
        self._request_times: list[float] = []

    def reserve(self, estimated_tokens: int) -> None:
        if estimated_tokens < 0:
            raise LLMError("LLM_CONFIG_INVALID")
        with self._lock:
            today = _utc_day()
            if self._day != today:
                self._day = today
                self._tokens_used = 0
            now = time.time()
            self._request_times = [t for t in self._request_times if now - t < 60.0]
            if len(self._request_times) >= self.max_requests_per_minute:
                raise LLMError("LLM_RATE_LIMITED")
            if self._tokens_used + estimated_tokens > self.daily_token_budget:
                raise LLMError("LLM_BUDGET_EXCEEDED")
            self._request_times.append(now)
            self._tokens_used += estimated_tokens

    def record_usage(self, actual_tokens: int, estimated_tokens: int) -> None:
        with self._lock:
            today = _utc_day()
            if self._day != today:
                self._day = today
                self._tokens_used = max(0, actual_tokens)
                return
            self._tokens_used = max(
                0, self._tokens_used - estimated_tokens + max(0, actual_tokens)
            )


class FileBudgetLedger:
    """POSIX flock + atomic replace ledger shared across Uvicorn API workers."""

    def __init__(
        self,
        root: Path,
        *,
        daily_token_budget: int,
        max_requests_per_minute: int,
    ) -> None:
        if daily_token_budget < 1 or max_requests_per_minute < 1:
            raise LLMError("LLM_CONFIG_INVALID")
        self.root = root
        self.daily_token_budget = daily_token_budget
        self.max_requests_per_minute = max_requests_per_minute

    @contextmanager
    def _locked_area(self) -> Iterator[int]:
        try:
            root_fd = storage.open_root(self.root)
            try:
                llm_fd = storage.open_area(root_fd, "llm")
            finally:
                os.close(root_fd)
        except (FileRejected, OSError):
            raise LLMError("LLM_STORAGE_ERROR") from None

        try:
            lock_fd = os.open(
                "budget.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=llm_fd,
            )
        except OSError:
            os.close(llm_fd)
            raise LLMError("LLM_STORAGE_ERROR") from None

        try:
            info = os.fstat(lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise LLMError("LLM_STORAGE_ERROR")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield llm_fd
        except LLMError:
            raise
        except OSError:
            raise LLMError("LLM_STORAGE_ERROR") from None
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)
            os.close(llm_fd)

    def _read_state(self, llm_fd: int) -> dict[str, object]:
        try:
            fd = os.open(
                "budget.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=llm_fd,
            )
        except FileNotFoundError:
            return {"day": _utc_day(), "tokens": 0, "requests": []}
        except OSError:
            raise LLMError("LLM_STORAGE_ERROR") from None
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_size > 1_048_576
            ):
                raise LLMError("LLM_STORAGE_ERROR")
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(1_048_577)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise LLMError("LLM_STORAGE_ERROR")
            return data
        except LLMError:
            raise
        except (OSError, ValueError, UnicodeError):
            raise LLMError("LLM_STORAGE_ERROR") from None

    def _write_state(self, llm_fd: int, state: dict[str, object]) -> None:
        payload = json.dumps(state, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        tmp_name = f"budget.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            fd = os.open(
                tmp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=llm_fd,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, "budget.json", src_dir_fd=llm_fd, dst_dir_fd=llm_fd)
            os.fsync(llm_fd)
        except OSError:
            try:
                os.unlink(tmp_name, dir_fd=llm_fd)
            except OSError:
                pass
            raise LLMError("LLM_STORAGE_ERROR") from None

    def reserve(self, estimated_tokens: int) -> None:
        if estimated_tokens < 0:
            raise LLMError("LLM_CONFIG_INVALID")
        with self._locked_area() as llm_fd:
            state = self._read_state(llm_fd)
            today = _utc_day()
            tokens = (
                int(state.get("tokens", 0))  # type: ignore[call-overload]
                if state.get("day") == today
                else 0
            )
            now = time.time()
            raw_reqs = state.get("requests", [])
            requests = [
                float(ts)
                for ts in (raw_reqs if isinstance(raw_reqs, list) else [])
                if isinstance(ts, (int, float)) and now - float(ts) < 60.0
            ]
            if len(requests) >= self.max_requests_per_minute:
                raise LLMError("LLM_RATE_LIMITED")
            if tokens + estimated_tokens > self.daily_token_budget:
                raise LLMError("LLM_BUDGET_EXCEEDED")
            requests.append(now)
            self._write_state(
                llm_fd,
                {
                    "day": today,
                    "tokens": tokens + estimated_tokens,
                    "requests": requests,
                },
            )

    def record_usage(self, actual_tokens: int, estimated_tokens: int) -> None:
        with self._locked_area() as llm_fd:
            state = self._read_state(llm_fd)
            today = _utc_day()
            tokens = (
                int(state.get("tokens", 0))  # type: ignore[call-overload]
                if state.get("day") == today
                else 0
            )
            adjusted = max(0, tokens - estimated_tokens + max(0, actual_tokens))
            now = time.time()
            raw_reqs = state.get("requests", [])
            requests = [
                float(ts)
                for ts in (raw_reqs if isinstance(raw_reqs, list) else [])
                if isinstance(ts, (int, float)) and now - float(ts) < 60.0
            ]
            self._write_state(
                llm_fd,
                {"day": today, "tokens": adjusted, "requests": requests},
            )
