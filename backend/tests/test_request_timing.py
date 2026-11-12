"""Numeric diagnostics preserve exception behavior, isolation and pool limits."""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.pool import ConnectionPoolEntry

from app.core.api_errors import install_api_errors
from app.core.observability import checked_event
from app.core.timing import (
    RequestTiming,
    TimingMiddleware,
    committing,
    current,
    mark,
    projecting,
    projection,
    run_timed,
    span,
)
from app.core.timing_pool import TimedQueue, TimedQueuePool
from app.core.timing_schema import checked_timings

CANARY = "private-contact@example.invalid chat-token secret SQL /private/file"


@pytest.mark.parametrize("denied", [False, True])
def test_no_request_or_exception_serialization(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, denied: bool
) -> None:
    monkeypatch.setenv("PERF001_TIMING", "1")
    app = FastAPI()
    install_api_errors(app)
    app.add_middleware(TimingMiddleware)

    def work(value: str) -> dict[str, bool]:
        assert value == CANARY
        with span("sql_ms", count="sql_calls"):
            if denied:
                raise HTTPException(403, CANARY)
        return {"ok": True}

    @app.post("/api/v1/chat/probe/{identifier}")
    async def route(identifier: str, body: dict[str, str]) -> dict[str, bool]:
        assert identifier == "private-identifier"
        mark("route_start_ns")
        return await run_timed(work, body["contact"])

    with (
        caplog.at_level(logging.INFO, logger="akbrizcase.runtime"),
        TestClient(app) as client,
    ):
        response = client.post(
            "/api/v1/chat/probe/private-identifier",
            json={"contact": CANARY},
            headers={"X-Request-ID": CANARY, "X-Chat-Token": CANARY},
        )
    assert response.status_code == (403 if denied else 200)
    events = [
        checked_event(json.loads(r.getMessage()))
        for r in caplog.records
        if r.name == "akbrizcase.runtime"
    ]
    phases = [e for e in events if e["event_code"] == "HTTP_PHASES"]
    assert len(phases) == 1
    event = phases[0]
    assert event["request_id"] == response.headers["X-Request-ID"]
    assert event["status"] == response.status_code
    metrics = checked_timings(event["timings"])
    assert metrics["worker_calls"] == metrics["sql_calls"] == 1
    assert metrics["worker_ms"] >= metrics["sql_ms"] > 0
    assert (
        metrics["response_end_ns"]
        >= metrics["response_start_ns"]
        >= metrics["asgi_start_ns"]
        > 0
    )
    exported = json.dumps(events)
    assert CANARY not in exported and "private-identifier" not in exported
    assert current.get() is None


def test_disabled_has_no_extra_events(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("PERF001_TIMING", raising=False)
    app = FastAPI()
    install_api_errors(app)
    app.add_middleware(TimingMiddleware)

    @app.get("/healthz")
    async def route() -> dict[str, bool]:
        assert current.get() is None
        return {"ok": True}

    with (
        caplog.at_level(logging.INFO, logger="akbrizcase.runtime"),
        TestClient(app) as client,
    ):
        assert client.get("/healthz").status_code == 200
    assert all(
        '"event_code":"HTTP_PHASES"' not in r.getMessage() for r in caplog.records
    )


def test_parallel_dispatch_and_failed_spans_are_isolated() -> None:
    async def one(index: int) -> RequestTiming:
        trace = RequestTiming()
        token = current.set(trace)

        def work() -> int:
            assert current.get() is trace
            with projection(), span("commit_ms", count="commit_calls"):
                assert committing.get() and projecting.get()
                with span("sql_ms", count="sql_calls"):
                    if index % 2:
                        raise ValueError(CANARY)
            return index

        try:
            if index % 2:
                with pytest.raises(ValueError, match="private-contact"):
                    await run_timed(work)
            else:
                assert await run_timed(work) == index
            assert not projecting.get() and not committing.get()
            assert (
                trace.metrics["worker_calls"]
                == trace.metrics["commit_calls"]
                == trace.metrics["sql_calls"]
                == 1
            )
            assert (
                trace.metrics["chat_projection_ms"]
                >= trace.metrics["projection_commit_ms"]
                > 0
            )
            return trace
        finally:
            current.reset(token)

    async def gather() -> list[RequestTiming]:
        return await asyncio.gather(*(one(i) for i in range(20)))

    traces = asyncio.run(gather())
    assert len({id(trace) for trace in traces}) == 20
    assert current.get() is None


@pytest.mark.parametrize("timeout", [False, True])
def test_pool_keeps_capacity_timeout_and_release(
    monkeypatch: pytest.MonkeyPatch, timeout: bool
) -> None:
    pool = TimedQueuePool(lambda: Mock(), pool_size=1, max_overflow=0, timeout=0.1)
    owner = pool.connect()
    entered = Event()
    original = TimedQueue.get

    def notified(
        self: TimedQueue, block: bool = True, timeout: float | None = None
    ) -> ConnectionPoolEntry:
        if block:
            entered.set()
        return original(self, block, timeout)

    monkeypatch.setattr(TimedQueue, "get", notified)

    def competing() -> RequestTiming:
        trace = RequestTiming()
        token = current.set(trace)
        try:
            if timeout:
                with pytest.raises(PoolTimeout):
                    pool.connect()
            else:
                connection = pool.connect()
                connection.close()
            return trace
        finally:
            current.reset(token)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(competing)
            assert entered.wait(1)
            assert pool.checkedout() == 1 and not future.done()
            if not timeout:
                owner.close()
            trace = future.result(2)
        assert trace.metrics["pool_acquire_ms"] >= trace.metrics["pool_queue_ms"] > 0
        assert trace.metrics["new_connections"] == 0
        assert pool.size() == 1 and pool._max_overflow == 0 and pool.timeout() == 0.1
    finally:
        owner.close()
        pool.dispose()


@pytest.mark.parametrize("value", [CANARY, float("nan"), float("inf"), -1, True])
def test_schema_rejects_unsafe_or_invalid_values(value: object) -> None:
    payload: dict[str, object] = dict(RequestTiming().metrics)
    payload["sql_ms"] = value
    with pytest.raises(ValueError, match="TIMING_SCHEMA_INVALID") as caught:
        checked_timings(payload)
    assert CANARY not in str(caught.value)
