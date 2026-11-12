"""Optional numeric timing, with no access to headers, SQL parameters or bodies.

Enabled only in the owned MVP-048 synthetic fixture. The normal HTTP timer,
thread limiter, transaction lifecycle and error propagation remain unchanged.
"""

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic_ns, process_time_ns, thread_time_ns
from uuid import UUID

from anyio.to_thread import current_default_thread_limiter
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.observability import emit, endpoint_category
from app.core.timing_schema import METRICS


@dataclass(repr=False)
class RequestTiming:
    metrics: dict[str, int | float] = field(
        default_factory=lambda: dict.fromkeys(METRICS, 0)
    )
    request_id: UUID | None = None
    pool_get_ended_ns: int = 0
    lock: Lock = field(default_factory=Lock)

    def add(self, name: str, value: int | float) -> None:
        with self.lock:
            self.metrics[name] += value


current: ContextVar[RequestTiming | None] = ContextVar("request_timing", default=None)
committing: ContextVar[bool] = ContextVar("timing_commit", default=False)
projecting: ContextVar[bool] = ContextVar("timing_projection", default=False)


def enabled() -> bool:
    return os.environ.get("PERF001_TIMING") == "1"


def mark(name: str) -> None:
    trace = current.get()
    if trace is not None:
        trace.metrics[name] = monotonic_ns()


@contextmanager
def span(name: str, *, count: str | None = None) -> Iterator[None]:
    trace = current.get()
    if trace is None:
        yield
        return
    started = monotonic_ns()
    process_cpu = process_time_ns() if name == "argon2_ms" else None
    commit_token = committing.set(True) if name == "commit_ms" else None
    try:
        yield
    finally:
        elapsed = (monotonic_ns() - started) / 1_000_000
        trace.add(name, elapsed)
        if process_cpu is not None:
            trace.add(
                "argon2_process_cpu_ms", (process_time_ns() - process_cpu) / 1_000_000
            )
        if name == "pool_acquire_ms" and committing.get():
            trace.add("commit_pool_ms", elapsed)
        if commit_token is not None:
            committing.reset(commit_token)
        if count is not None:
            trace.add(count, 1)
        if name == "commit_ms" and projecting.get():
            trace.add("projection_commit_ms", elapsed)


@contextmanager
def projection() -> Iterator[None]:
    token = projecting.set(True)
    try:
        with span("chat_projection_ms"):
            yield
    finally:
        projecting.reset(token)


async def run_timed[T, **P](
    function: "Callable[P, T]", *args: P.args, **kwargs: P.kwargs
) -> T:
    trace = current.get()
    if trace is None:
        return await run_in_threadpool(function, *args, **kwargs)
    limiter = current_default_thread_limiter()
    trace.metrics["thread_tokens_before"] = limiter.borrowed_tokens
    trace.metrics["thread_tokens_total"] = int(limiter.total_tokens)
    queued = monotonic_ns()

    def execute() -> T:
        started, cpu = monotonic_ns(), thread_time_ns()
        trace.add("dispatch_ms", (started - queued) / 1_000_000)
        trace.add("worker_calls", 1)
        try:
            return function(*args, **kwargs)
        finally:
            trace.add("worker_ms", (monotonic_ns() - started) / 1_000_000)
            trace.add("worker_cpu_ms", (thread_time_ns() - cpu) / 1_000_000)

    return await run_in_threadpool(execute)


class TimingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not enabled():
            await self.app(scope, receive, send)
            return
        trace = RequestTiming()
        trace.metrics["process_key"] = os.getpid()
        trace.metrics["asgi_start_ns"] = monotonic_ns()
        trace.metrics["process_start_cpu_ns"] = process_time_ns()
        token = current.set(trace)
        status = 500

        async def observed_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                trace.metrics["response_start_ns"] = monotonic_ns()
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                trace.metrics["response_end_ns"] = monotonic_ns()

        try:
            await self.app(scope, receive, observed_send)
        finally:
            trace.metrics["process_end_cpu_ns"] = process_time_ns()
            current.reset(token)
            if trace.request_id is not None:
                duration = (monotonic_ns() - trace.metrics["asgi_start_ns"]) / 1_000_000
                emit(
                    "HTTP_PHASES",
                    request_id=str(trace.request_id),
                    status=status,
                    endpoint=endpoint_category(scope["path"]),
                    duration_ms=duration,
                    timings=trace.metrics,
                )
