"""Real PostgreSQL 16 and two independent Uvicorn processes, never ASGI mocks."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session
from test_order_creation import (
    counts,
    creation_client,
    creation_db,
    order_headers,
    support_body,
)

from app.core.db import Database
from app.core.db_types import utc_now
from app.core.rate_limits import (
    IpSecurity,
    RateLimitBucket,
    RateLimited,
    check_limit,
    cleanup_buckets,
)
from app.modules.chat.models import ChatSession
from app.modules.orders.erasure_service import erase_order_pii
from app.modules.orders.models import Order

pytestmark = pytest.mark.integration
# Re-export the existing synthetic creation fixture without replacing its guards.
__all__ = ["creation_client", "creation_db"]


@dataclass
class Result:
    status: int
    body: Any
    headers: dict[str, str]


@dataclass
class Api:
    origin: str
    pid: int

    def request(
        self,
        path: str,
        body: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> Result:
        request = Request(
            self.origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            response = urlopen(request, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            return Result(
                response.status,
                json.load(response),
                {k.lower(): v for k, v in response.headers.items()},
            )


@contextmanager
def api_process() -> Iterator[Api]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-proxy-headers",
            "--no-access-log",
            "--log-level",
            "critical",
        ],
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    api = Api(f"http://127.0.0.1:{port}", process.pid)
    try:
        deadline = time.monotonic() + 15
        while True:
            assert process.poll() is None, "Real API process exited during startup"
            try:
                if api.request("/readyz").status == 200:
                    break
            except (URLError, OSError):
                pass
            assert time.monotonic() < deadline, "Real API process did not become ready"
            time.sleep(0.05)
        yield api
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        # Uvicorn re-raises the captured shutdown signal after lifespan cleanup.
        # SIGKILL/timeout or any other process failure remains an error.
        assert process.returncode in (0, -signal.SIGTERM)


@pytest.fixture
def apis(
    creation_db: Database, tmp_path: Path, monkeypatch: MonkeyPatch
) -> Iterator[tuple[Api, Api]]:
    secret = tmp_path / "order-key"
    secret.write_text(
        json.dumps(
            {
                "active": "synthetic-process-order-secret-" * 3,
                "previous": ["synthetic-next-process-order-secret-" * 3],
            }
        )
    )
    secret.chmod(0o600)
    monkeypatch.setenv("ORDER_HMAC_SECRET_FILE", str(secret))
    monkeypatch.delenv("TRUSTED_PROXY_HOST", raising=False)
    with api_process() as first, api_process() as second:
        assert first.pid != second.pid
        with creation_db.unit_of_work() as session:
            session.execute(delete(ChatSession))
        try:
            yield first, second
        finally:
            with creation_db.unit_of_work() as session:
                session.execute(delete(ChatSession))


def limited(result: Result, maximum: int) -> None:
    assert result.status == 429, result.body
    assert 1 <= int(result.headers["retry-after"]) <= maximum
    assert result.headers["cache-control"] == "no-store"
    assert result.body["error"] == {
        "code": "RATE_LIMITED",
        "message": "Too many requests",
        "details": {},
    }
    assert str(UUID(result.body["request_id"])) == result.headers["x-request-id"]
    serialized = json.dumps(result.body)
    for value in ("127.0.0.1", "synthetic", "subject_hash", "secret", "token"):
        assert value not in serialized


@pytest.mark.parametrize("scope,limit", [("session", 10), ("order", 5)])
def test_concurrent_two_process_ip_limits_and_spoofing(
    apis: tuple[Api, Api],
    creation_db: Database,
    scope: str,
    limit: int,
) -> None:
    path = "/api/v1/chat/sessions" if scope == "session" else "/api/v1/orders"

    def submit(index: int) -> Result:
        headers = order_headers() | {
            "X-Forwarded-For": f"198.51.100.{index + 1}",
            "Forwarded": f"for=198.51.100.{index + 1};proto=https",
            "X-Real-IP": f"198.51.100.{index + 1}",
            "X-Akbriz-Client-IP": f"198.51.100.{index + 1}",
            "X-Akbriz-Scheme": "https",
        }
        return apis[index % 2].request(
            path,
            {"mode": "diagnose"} if scope == "session" else support_body(),
            headers,
        )

    with ThreadPoolExecutor(max_workers=limit + 4) as pool:
        results = list(pool.map(submit, range(limit + 4)))
    assert sorted(r.status for r in results) == [201] * limit + [429] * 4
    for result in results:
        if result.status == 429:
            limited(result, 3600)
    security = IpSecurity.from_environment()
    assert security is not None
    with creation_db.unit_of_work() as session:
        rows = list(
            session.scalars(
                select(RateLimitBucket).where(
                    RateLimitBucket.scope == scope,
                    RateLimitBucket.subject_hash.in_(
                        security.subjects(
                            scope, "127.0.0.1", int(utc_now().timestamp()) // 3600
                        )
                    ),
                )
            )
        )
        assert len(rows) == 2 and {r.used for r in rows} == {limit}
        assert 0 < (rows[0].expires_at - rows[0].started_at).total_seconds() <= 3600
        assert "127.0.0.1" not in repr(rows[0].__dict__)
        if scope == "session":
            assert (
                session.scalar(select(func.count()).select_from(ChatSession)) == limit
            )
        else:
            assert counts(creation_db) == (limit, limit, 0, limit, limit)


def test_full_order_quota_preserves_authenticated_replays_and_hides_keys(
    apis: tuple[Api, Api],
    creation_db: Database,
) -> None:
    first, second = apis
    headers, body = order_headers(), support_body()
    original = first.request("/api/v1/orders", body, headers)
    assert original.status == 201
    for _ in range(4):
        assert second.request("/api/v1/orders", body, order_headers()).status == 201
    for api in apis:
        repeated = api.request("/api/v1/orders", body, headers)
        assert repeated.status == 200 and repeated.body == original.body
        changed = api.request(
            "/api/v1/orders", body | {"comment": "Synthetic changed"}, headers
        )
        assert changed.status == 409
        assert changed.body["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        for candidate in (headers, order_headers()):
            wrong = api.request(
                "/api/v1/orders", body, candidate | {"X-Request-Token": "b" * 64}
            )
            limited(wrong, 3600)
        malformed = api.request(
            "/api/v1/orders", body, headers | {"X-Request-Token": "bad"}
        )
        assert malformed.status == 422 and "retry-after" not in malformed.headers
    with creation_db.unit_of_work() as session:
        order = session.scalar(
            select(Order).where(
                Order.idempotency_key == UUID(headers["Idempotency-Key"])
            )
        )
        assert order is not None
        erase_order_pii(session, order.id, utc_now())
    limited(second.request("/api/v1/orders", body, headers), 3600)
    assert counts(creation_db) == (5, 5, 0, 5, 5)


def test_same_key_race_at_last_slot_charges_one_creation(
    apis: tuple[Api, Api],
    creation_db: Database,
) -> None:
    for _ in range(4):
        assert (
            apis[0].request("/api/v1/orders", support_body(), order_headers()).status
            == 201
        )
    headers = order_headers()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda i: apis[i % 2].request(
                    "/api/v1/orders", support_body(), headers
                ),
                range(8),
            )
        )
    assert sorted(r.status for r in results) == [200] * 7 + [201], [
        r.status for r in results
    ]
    assert all(r.body == results[0].body for r in results)
    assert counts(creation_db) == (5, 5, 0, 5, 5)
    limited(apis[1].request("/api/v1/orders", support_body(), order_headers()), 3600)


def test_failed_validation_and_late_commit_rollback_cost_nothing(
    apis: tuple[Api, Api],
    creation_db: Database,
    databases: tuple[Database, Database],
) -> None:
    first, second = apis
    bad = support_body() | {
        "consent": {"version": "synthetic-absent", "accepted": True}
    }
    assert first.request("/api/v1/orders", bad, order_headers()).status == 409
    # Defer a real PostgreSQL constraint-trigger failure until COMMIT, after
    # all domain rows and the quota increment have been flushed.
    migrator = databases[1]
    with migrator.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE FUNCTION app.synthetic_limit_commit_failure() RETURNS "
                "trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Synthetic "
                "commit failure' USING ERRCODE='23514'; END $$"
            )
        )
        connection.execute(
            text(
                "CREATE CONSTRAINT TRIGGER synthetic_limit_failure AFTER INSERT ON "
                "app.orders DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE "
                "FUNCTION app.synthetic_limit_commit_failure()"
            )
        )
    try:
        failed = second.request("/api/v1/orders", support_body(), order_headers())
        assert failed.status == 503
        assert failed.body["error"]["details"] == {}
        assert "Synthetic" not in json.dumps(failed.body)
    finally:
        with migrator.engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER synthetic_limit_failure ON app.orders")
            )
            connection.execute(
                text("DROP FUNCTION app.synthetic_limit_commit_failure()")
            )
    assert counts(creation_db) == (0, 0, 0, 0, 0)
    for _ in range(5):
        assert (
            first.request("/api/v1/orders", support_body(), order_headers()).status
            == 201
        )
    limited(second.request("/api/v1/orders", support_body(), order_headers()), 3600)


def test_thirty_messages_two_processes_replay_and_next_window(
    apis: tuple[Api, Api],
    creation_db: Database,
) -> None:
    own = apis[0].request("/api/v1/chat/sessions", {"mode": "knowledge"})
    assert own.status == 201
    token = {"X-Chat-Token": own.body["session_token"]}
    message: dict[str, object] = {}
    for version in range(30):
        message = dict(
            session_id=own.body["session_id"],
            request_id=str(uuid4()),
            expected_version=version,
            input={"type": "select_mode", "mode": "knowledge"},
        )
        assert (
            apis[version % 2].request("/api/v1/chat/messages", message, token).status
            == 200
        )
    # Replays use no new-message quota even at 30. State conflicts cost nothing.
    assert apis[0].request("/api/v1/chat/messages", message, token).status == 200
    next_message = message | {"request_id": str(uuid4()), "expected_version": 30}
    limited(apis[1].request("/api/v1/chat/messages", next_message, token), 60)
    assert (
        apis[0]
        .request("/api/v1/chat/messages", next_message, {"X-Chat-Token": "b" * 64})
        .status
        == 404
    )
    with creation_db.unit_of_work() as session:
        session.execute(
            text(
                "UPDATE app.rate_limit_buckets SET started_at=started_at-interval "
                "'2 minutes', expires_at=expires_at-interval '2 minutes' WHERE "
                "scope='message'"
            )
        )
    assert apis[0].request("/api/v1/chat/messages", next_message, token).status == 200


@pytest.mark.parametrize(
    "scope,limit", [("session", 10), ("order", 5), ("message", 30)]
)
def test_counter_expiry_cleanup_rotation_and_rollback(
    creation_db: Database,
    scope: str,
    limit: int,
) -> None:
    old = IpSecurity.from_environment()
    assert old is not None
    from app.modules.orders.security import OrderSecurity

    rotated = IpSecurity(
        OrderSecurity(b"synthetic-new-IP-secret" * 3, (old.keyring.secret,))
    )
    before = old.subjects(scope, "192.0.2.78", 100)
    after = rotated.subjects(scope, "192.0.2.78", 100)
    for _ in range(limit - 1):
        with creation_db.unit_of_work() as session:
            check_limit(session, scope, before)
    with pytest.raises(RuntimeError), creation_db.unit_of_work() as session:
        check_limit(session, scope, after)
        raise RuntimeError("Synthetic rollback")
    with creation_db.unit_of_work() as session:
        check_limit(session, scope, after)
    for subjects in (before, after, rotated.subjects(scope, "192.0.2.78", 101)):
        with pytest.raises(RateLimited), creation_db.unit_of_work() as session:
            check_limit(session, scope, subjects)
    with creation_db.unit_of_work() as session:
        rows = list(
            session.scalars(
                select(RateLimitBucket).where(RateLimitBucket.subject_hash.in_(after))
            )
        )
        assert len(rows) == 4 and {r.used for r in rows} == {limit}
        assert len({r.expires_at for r in rows}) == 1
        session.execute(
            text(
                "UPDATE app.rate_limit_buckets SET started_at=started_at-interval "
                "'2 hours', expires_at=expires_at-interval '2 hours' WHERE "
                "subject_hash=ANY(:subjects)"
            ),
            {"subjects": list(after)},
        )
    with creation_db.unit_of_work() as session:
        cleanup_buckets(session)
        assert not list(
            session.scalars(
                select(RateLimitBucket).where(RateLimitBucket.subject_hash.in_(after))
            )
        )
        check_limit(session, scope, after)
    with creation_db.unit_of_work() as session:
        assert {
            r.used
            for r in session.scalars(
                select(RateLimitBucket).where(RateLimitBucket.subject_hash.in_(after))
            )
        } == {1}


def test_rolling_file_rotation_two_processes_preserves_both_replay_directions(
    apis: tuple[Api, Api],
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    # Phase A readers already know the verification-only future order key.
    # Phase B switches the writer, retaining the old verification key.
    body, original_headers = support_body(), order_headers()
    original = apis[0].request("/api/v1/orders", body, original_headers)
    assert original.status == 201
    for _ in range(3):
        assert apis[1].request("/api/v1/orders", body, order_headers()).status == 201
    ip_old = IpSecurity.from_environment()
    assert ip_old is not None
    ip_path = tmp_path / "rotated-ip-ring"
    ip_path.write_text(
        json.dumps(
            {
                "active": "synthetic-new-process-IP-key-" * 3,
                "previous": [ip_old.keyring.secret.decode("ascii")],
            }
        )
    )
    ip_path.chmod(0o600)
    order_path = tmp_path / "rotated-order-ring"
    order_path.write_text(
        json.dumps(
            {
                "active": "synthetic-next-process-order-secret-" * 3,
                "previous": ["synthetic-process-order-secret-" * 3],
            }
        )
    )
    order_path.chmod(0o600)
    monkeypatch.setenv("IP_HMAC_SECRET_FILE", str(ip_path))
    monkeypatch.setenv("ORDER_HMAC_SECRET_FILE", str(order_path))
    with api_process() as rotated:
        assert rotated.pid not in {api.pid for api in apis}
        repeated = rotated.request("/api/v1/orders", body, original_headers)
        assert repeated.status == 200 and repeated.body == original.body
        new_headers = order_headers()
        created = rotated.request("/api/v1/orders", body, new_headers)
        assert created.status == 201
        # A phase-A process must also verify the new writer's request hash.
        older_reader = apis[0].request("/api/v1/orders", body, new_headers)
        assert older_reader.status == 200 and older_reader.body == created.body
        for api in (apis[1], rotated):
            limited(api.request("/api/v1/orders", body, order_headers()), 3600)
            assert api.request("/api/v1/orders", body, original_headers).status == 200


def test_winner_commits_between_unused_lookup_and_full_quota_precheck(
    creation_client: TestClient,
    creation_db: Database,
    monkeypatch: MonkeyPatch,
) -> None:
    for _ in range(4):
        assert (
            creation_client.post(
                "/api/v1/orders", json=support_body(), headers=order_headers()
            ).status_code
            == 201
        )
    paused, resume = Event(), Event()

    def gate(
        session: Session, scope: str, subjects: tuple[str, ...], *, consume: bool = True
    ) -> None:
        # Schedule only; every actual quota decision still uses PostgreSQL.
        if not consume and not paused.is_set():
            paused.set()
            assert resume.wait(10)
        check_limit(session, scope, subjects, consume=consume)

    monkeypatch.setattr("app.modules.orders.router.check_limit", gate)
    headers = order_headers()
    with ThreadPoolExecutor(max_workers=1) as pool:
        loser = pool.submit(
            creation_client.post, "/api/v1/orders", json=support_body(), headers=headers
        )
        assert paused.wait(5)
        try:
            winner = creation_client.post(
                "/api/v1/orders", json=support_body(), headers=headers
            )
            assert winner.status_code == 201
        finally:
            resume.set()
        repeated = loser.result(timeout=10)
    assert repeated.status_code == 200
    assert repeated.json() == winner.json()
    assert counts(creation_db) == (5, 5, 0, 5, 5)
