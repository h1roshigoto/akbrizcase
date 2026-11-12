"""Real PG16 timing: bounded pools, transactional failures and fresh projection."""

import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

import pytest
from commercial_helpers import pg_fault
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import ConnectionPoolEntry
from test_order_creation import commercial, counts, order_headers
from test_order_creation import creation_db as creation_db

from app.core.db import Database, DatabaseSettings
from app.core.observability import checked_event
from app.core.timing import RequestTiming, current
from app.core.timing_pool import TimedQueue, TimedQueuePool
from app.main import app
from app.modules.orders.security import OrderSecurity

pytestmark = pytest.mark.integration
__all__ = ["creation_db", "observed_db"]


@pytest.fixture(params=[False, True], ids=["ordinary-pool", "api-pool"])
def observed_db(
    monkeypatch: pytest.MonkeyPatch,
    databases: tuple[Database, Database],
    request: pytest.FixtureRequest,
) -> Iterator[Database]:
    # Reuse the sentinel-guarded integration DB, with a new normal factory/pool.
    monkeypatch.setenv("PERF001_TIMING", "1")
    database = Database(DatabaseSettings.from_environment(), api=request.param)
    try:
        assert database.ready()
        yield database
    finally:
        database.close()


def phases(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    events = [
        checked_event(json.loads(r.getMessage()))
        for r in caplog.records
        if r.name == "akbrizcase.runtime"
    ]
    return [e for e in events if e["event_code"] == "HTTP_PHASES"]


@pytest.mark.parametrize("fault", ["flush", "commit"])
def test_timing_preserves_order_replay_denial_and_full_rollback(
    creation_db: Database,
    observed_db: Database,
    caplog: pytest.LogCaptureFixture,
    fault: str,
) -> None:
    body, headers = commercial(creation_db, ("DEMO-S-001",)), order_headers()
    with (
        caplog.at_level(logging.INFO, logger="akbrizcase.runtime"),
        TestClient(app, client=("192.0.2.1", 50000)) as client,
    ):
        app.state.database = observed_db
        app.state.order_security = OrderSecurity(b"synthetic-observed-order-key" * 2)
        first = client.post("/api/v1/orders", json=body, headers=headers)
        assert first.status_code == 201
        replay = client.post("/api/v1/orders", json=body, headers=headers)
        assert replay.status_code == 200 and replay.json() == first.json()
        denied = client.post(
            "/api/v1/orders", json=body, headers=headers | {"X-Request-Token": "b" * 64}
        )
        assert denied.status_code == 404
        with pg_fault(observed_db, "email_outbox", commit=fault == "commit"):
            failed = client.post("/api/v1/orders", json=body, headers=order_headers())
        assert failed.status_code == 503
        assert counts(creation_db) == (1, 1, 1, 1, 1)
    events = phases(caplog)
    assert len(events) == 4
    for response, event in zip((first, replay, denied, failed), events, strict=True):
        assert event["request_id"] == response.headers["X-Request-ID"]
        assert event["status"] == response.status_code
        metrics = event["timings"]
        assert metrics["worker_calls"] == 1 and metrics["sql_calls"] > 0
        assert metrics["pool_checkouts"] >= 1
        assert metrics["pool_acquire_ms"] >= metrics["pool_queue_ms"] >= 0
    assert events[0]["timings"]["commit_calls"] == 1
    assert events[1]["timings"]["commit_calls"] == 1
    assert events[2]["timings"]["rollback_calls"] == 1
    assert events[3]["timings"]["rollback_calls"] == 1
    assert "synthetic@example.invalid" not in json.dumps(events)
    assert isinstance(observed_db.engine.pool, TimedQueuePool)
    assert observed_db.engine.pool.checkedout() == 0


def test_real_pool_wait_at_configured_connection_cap(
    observed_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = observed_db.engine
    assert isinstance(engine.pool, TimedQueuePool)
    capacity = engine.pool.size() + engine.pool._max_overflow
    assert (engine.pool.size(), engine.pool._max_overflow) in {(5, 5), (8, 0)}
    held = [engine.connect() for _ in range(capacity)]
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
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT 1")) == 1
            return trace
        finally:
            current.reset(token)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(competing)
            assert entered.wait(2)
            assert engine.pool.checkedout() == capacity and not future.done()
            held[0].close()
            trace = future.result(3)
        assert trace.metrics["pool_queue_ms"] > 0
        assert trace.metrics["pool_acquire_ms"] >= trace.metrics["pool_queue_ms"]
        assert trace.metrics["new_connections"] == 0
        assert trace.metrics["pool_checkouts"] == trace.metrics["sql_calls"] == 1
        assert engine.pool.size() + engine.pool._max_overflow == capacity
        assert engine.pool.timeout() == 3
    finally:
        for connection in held:
            connection.close()
    assert engine.pool.checkedout() == 0
