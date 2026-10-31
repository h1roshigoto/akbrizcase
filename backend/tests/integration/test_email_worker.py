"""Real PG16 locks/commits, SMTP wire data, retry and crash boundaries."""

import json
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from email import policy
from email.parser import Parser
from threading import Barrier, Event
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from smtp_double import Sink, start_sink
from sqlalchemy import event, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from test_order_creation import commercial, order_headers
from test_order_creation import creation_client as creation_client
from test_order_creation import creation_db as creation_db

from app.core.db import Database
from app.modules.notify.models import EmailOutbox
from app.modules.notify.outbox import (
    LEASE,
    RETRY,
    Claim,
    claim_due,
    database_now,
    finish_failure,
    finish_success,
    recover_expired,
)
from app.modules.notify.smtp import SendFailure, SMTPSettings, send_notification
from app.worker.runner import tick

pytestmark = pytest.mark.integration


@pytest.fixture
def sink() -> Iterator[Sink]:
    server, thread = start_sink()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(3)


def configuration(sink: Sink) -> SMTPSettings:
    return SMTPSettings(
        "127.0.0.1",
        sink.server_address[1],
        "test",
        "notify@example.invalid",
        "manager@example.invalid",
        "https://staff.example.invalid",
        frozenset({"https://staff.example.invalid"}),
        synthetic=True,
    )


def create(
    client: TestClient, database: Database
) -> tuple[dict[str, object], dict[str, str], dict[str, object]]:
    body = commercial(database, ("DEMO-P-001", "DEMO-S-001"))
    headers = order_headers()
    response = client.post("/api/v1/orders", json=body, headers=headers)
    assert response.status_code == 201
    return body, headers, response.json()


def row(database: Database) -> EmailOutbox:
    with database.unit_of_work() as session:
        return session.scalars(select(EmailOutbox)).one()


def claim(database: Database) -> Claim:
    with database.unit_of_work() as session:
        result = claim_due(session)
    assert result is not None
    return result


def expire(database: Database) -> None:
    with database.unit_of_work() as session:
        session.execute(
            update(EmailOutbox).values(lease_until=database_now(session) - LEASE)
        )


def due(database: Database) -> None:
    with database.unit_of_work() as session:
        session.execute(
            update(EmailOutbox).values(next_attempt_at=database_now(session) - LEASE)
        )


def private_rows(database: Database) -> str:
    with database.unit_of_work() as session:
        return json.dumps(
            {
                table: list(
                    session.scalars(
                        text(f"SELECT row_to_json(t) FROM app.{table} t ORDER BY id")
                    )
                )
                for table in (
                    "orders",
                    "consents",
                    "order_items",
                    "order_status_history",
                )
            },
            default=str,
        )


def test_success_wire_mime_no_connection_and_immutable_replay(
    creation_client: TestClient,
    creation_db: Database,
    sink: Sink,
) -> None:
    body, headers, receipt = create(creation_client, creation_db)
    before = private_rows(creation_db)

    def send(settings: SMTPSettings, item: Claim) -> None:
        assert creation_db.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        assert row(creation_db).status == "sending"
        send_notification(settings, item)

    assert tick(creation_db, configuration(sink), send)
    result = row(creation_db)
    assert result.status == "sent" and result.attempts == 1
    assert result.sent_at is not None and result.sent_at.tzinfo is not None
    assert result.lease_until is None and result.lease_token is None
    assert result.last_error_code is None
    assert len(sink.messages) == 1
    mime = Parser(policy=policy.default).parsestr(sink.messages[0])
    assert str(mime["To"]) == "manager@example.invalid"
    assert mime.get_content() == (
        f"Order #{receipt['number']}\r\nhttps://staff.example.invalid/admin/orders/{result.order_id}\r\n"
    )
    assert str(mime["Message-ID"]) == f"<outbox.{result.id}@example.invalid>"
    assert private_rows(creation_db) == before, "Business rows changed"
    replay = creation_client.post("/api/v1/orders", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == receipt
    assert private_rows(creation_db) == before, "Replay changed rows"
    assert not tick(creation_db, configuration(sink), send)
    with creation_db.unit_of_work() as session:
        metadata = str(
            list(session.scalars(text("SELECT row_to_json(t) FROM app.email_outbox t")))
        )
    for value in (
        "Synthetic Contact",
        "synthetic@example.invalid",
        str(body["comment"]),
        headers["X-Request-Token"],
    ):
        assert value not in sink.messages[0] + metadata


def test_disabled_not_due_and_claim_server_lease(
    creation_client: TestClient,
    creation_db: Database,
) -> None:
    create(creation_client, creation_db)
    assert not tick(creation_db, None, lambda *_: pytest.fail("Disabled SMTP called"))
    assert row(creation_db).attempts == 0
    with creation_db.unit_of_work() as session:
        now = database_now(session)
        session.execute(update(EmailOutbox).values(next_attempt_at=now + LEASE))
        assert claim_due(session) is None
    due(creation_db)
    result = claim(creation_db)
    assert result.attempts == 1
    with creation_db.unit_of_work() as session:
        stored = session.get(EmailOutbox, result.id)
        assert stored is not None and stored.lease_until is not None
        assert (
            timedelta(seconds=118) < stored.lease_until - database_now(session) <= LEASE
        )
        assert stored.lease_token == result.token
        assert claim_due(session) is None


def test_retry_matrix_and_terminal_failure(
    creation_client: TestClient,
    creation_db: Database,
    sink: Sink,
) -> None:
    create(creation_client, creation_db)
    sink.mode = "reject"
    for attempt in range(1, 6):
        assert tick(creation_db, configuration(sink), send_notification)
        stored = row(creation_db)
        assert stored.attempts == attempt and stored.last_error_code == "SMTP_REJECTED"
        assert stored.lease_token is None and stored.lease_until is None
        assert stored.sent_at is None
        if attempt < 5:
            assert stored.status == "pending"
            with creation_db.unit_of_work() as session:
                delay = stored.next_attempt_at - database_now(session)
                assert (
                    RETRY[attempt - 1] - timedelta(seconds=2)
                    <= delay
                    <= RETRY[attempt - 1]
                )
                assert claim_due(session) is None
            due(creation_db)
        else:
            assert stored.status == "failed"
    assert not tick(creation_db, configuration(sink), send_notification)
    assert row(creation_db).attempts == 5 and sink.messages == []


@pytest.mark.parametrize("after_smtp", [False, True])
def test_crash_before_and_after_smtp(
    creation_client: TestClient,
    creation_db: Database,
    sink: Sink,
    after_smtp: bool,
) -> None:
    create(creation_client, creation_db)
    # Terminate the actual process without finally blocks or result commit.
    script = (
        "import os; from app.core.db import Database, DatabaseSettings; "
        "from app.modules.notify.outbox import claim_due; "
        "from app.modules.notify.smtp import SMTPSettings, send_notification; "
        "db=Database(DatabaseSettings.from_environment()); "
        "\nwith db.unit_of_work() as session: item=claim_due(session)\n"
        "assert item is not None\n"
        f"settings=SMTPSettings('127.0.0.1', {sink.server_address[1]}, 'test', "
        "'notify@example.invalid', 'manager@example.invalid', "
        "'https://staff.example.invalid', "
        "frozenset({'https://staff.example.invalid'}), "
        "synthetic=True)\n"
        + ("send_notification(settings,item)\n" if after_smtp else "")
        + "os._exit(77)\n"
    )
    process = subprocess.run(
        [sys.executable, "-c", script], timeout=10, capture_output=True
    )
    assert process.returncode == 77, "Crash boundary process did not reach exit"
    assert row(creation_db).status == "sending"
    expire(creation_db)
    with creation_db.unit_of_work() as session:
        assert recover_expired(session) == 1
        assert recover_expired(session) == 0
    assert row(creation_db).attempts == 1 and row(creation_db).status == "pending"
    due(creation_db)
    assert tick(creation_db, configuration(sink), send_notification)
    assert len(sink.messages) == (2 if after_smtp else 1)
    if after_smtp:
        assert (
            Parser().parsestr(sink.messages[0])["Message-ID"]
            == Parser().parsestr(sink.messages[1])["Message-ID"]
        )
    assert row(creation_db).attempts == 2 and row(creation_db).status == "sent"


def test_fifth_expired_lease_has_no_sixth_claim(
    creation_client: TestClient,
    creation_db: Database,
) -> None:
    create(creation_client, creation_db)
    with creation_db.unit_of_work() as session:
        session.execute(update(EmailOutbox).values(attempts=4))
    assert claim(creation_db).attempts == 5
    expire(creation_db)
    with creation_db.unit_of_work() as session:
        assert recover_expired(session) == 1
        assert claim_due(session) is None
    assert row(creation_db).status == "failed" and row(creation_db).attempts == 5


@pytest.mark.parametrize("success", [True, False])
def test_stale_token_cas_and_erasure(
    creation_client: TestClient,
    creation_db: Database,
    success: bool,
    databases: tuple[Database, Database],
) -> None:
    create(creation_client, creation_db)
    first = claim(creation_db)
    expire(creation_db)
    with creation_db.unit_of_work() as session:
        assert recover_expired(session) == 1
    due(creation_db)
    second = claim(creation_db)
    before = row(creation_db).__dict__.copy()
    with creation_db.unit_of_work() as session:
        assert not (
            finish_success(session, first)
            if success
            else finish_failure(session, first, "SMTP_TIMEOUT")
        )
    after = row(creation_db).__dict__.copy()
    before.pop("_sa_instance_state")
    after.pop("_sa_instance_state")
    assert before == after
    with creation_db.unit_of_work() as session:
        assert (
            finish_success(session, second)
            if success
            else finish_failure(session, second, "SMTP_TIMEOUT")
        )
    assert row(creation_db).status == ("sent" if success else "pending")
    with databases[1].unit_of_work() as session:
        session.execute(
            text("DELETE FROM app.orders WHERE id=:id"), {"id": second.order_id}
        )
    with creation_db.unit_of_work() as session:
        assert not finish_success(session, second)
        assert not finish_failure(session, second, "SMTP_TIMEOUT")
        assert claim_due(session) is None
        assert session.get(EmailOutbox, second.id) is None


@pytest.mark.parametrize("recover", [False, True])
def test_real_parallel_skip_locked_and_unique_owner(
    creation_client: TestClient,
    creation_db: Database,
    recover: bool,
) -> None:
    create(creation_client, creation_db)
    if recover:
        claim(creation_db)
        expire(creation_db)
    barrier, locked, release = Barrier(2), Event(), Event()
    pids: list[int] = []

    def first() -> object:
        with creation_db.unit_of_work() as session:
            pids.append(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
            barrier.wait(5)
            value = recover_expired(session) if recover else claim_due(session)
            locked.set()
            assert release.wait(5)
            return value

    def second() -> object:
        with creation_db.unit_of_work() as session:
            pids.append(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
            barrier.wait(5)
            assert locked.wait(5)
            value = recover_expired(session) if recover else claim_due(session)
            return value

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        try:
            assert locked.wait(5)
            # Independent connection observes the actual row lock, not a mock.
            with creation_db.engine.connect() as observer:
                with pytest.raises(DBAPIError) as blocked:
                    observer.execute(
                        text("SELECT id FROM app.email_outbox FOR UPDATE NOWAIT")
                    )
                assert getattr(blocked.value.orig, "sqlstate", None) == "55P03"
            assert b.result(5) == (0 if recover else None)
        finally:
            release.set()
        value = a.result(5)
    assert len(set(pids)) == 2
    assert (value == 1) if recover else isinstance(value, Claim)
    assert row(creation_db).attempts == 1
    assert row(creation_db).status == ("pending" if recover else "sending")


@pytest.mark.parametrize("commit_number", [2, 3])
def test_real_commit_error_boundaries_and_db_recovery(
    creation_client: TestClient,
    creation_db: Database,
    sink: Sink,
    commit_number: int,
) -> None:
    create(creation_client, creation_db)
    commits = 0

    def fail(connection: Connection) -> None:
        nonlocal commits
        commits += 1
        # Synthetic marker read, recovery, claim, result each has its own commit.
        if commits == commit_number + 1:
            connection.exec_driver_sql("SELECT 1/0")

    event.listen(creation_db.engine, "commit", fail)
    try:
        with pytest.raises(DBAPIError):
            tick(creation_db, configuration(sink), send_notification)
    finally:
        event.remove(creation_db.engine, "commit", fail)
    stored = row(creation_db)
    if commit_number == 2:
        assert stored.status == "pending" and stored.attempts == 0
        assert not sink.messages
    else:
        assert stored.status == "sending" and stored.sent_at is None
        assert len(sink.messages) == 1
        expire(creation_db)
        with creation_db.unit_of_work() as session:
            recover_expired(session)
        due(creation_db)
    assert tick(creation_db, configuration(sink), send_notification)
    assert row(creation_db).status == "sent"


@pytest.mark.parametrize("mode", ["timeout", "slow"])
def test_real_smtp_timeout_and_safe_error(
    creation_client: TestClient,
    creation_db: Database,
    sink: Sink,
    mode: str,
) -> None:
    create(creation_client, creation_db)
    sink.mode = mode
    with patch("app.modules.notify.smtp.TIMEOUT", 0.1):
        assert tick(creation_db, configuration(sink), send_notification)
    assert row(creation_db).last_error_code == "SMTP_TIMEOUT"
    assert row(creation_db).status == "pending" and row(creation_db).attempts == 1


def test_unknown_failure_is_sanitized(
    creation_client: TestClient,
    creation_db: Database,
    sink: Sink,
) -> None:
    create(creation_client, creation_db)

    def fail(_: SMTPSettings, __: Claim) -> None:
        raise SendFailure("synthetic@example.invalid private body")

    assert tick(creation_db, configuration(sink), fail)
    assert row(creation_db).last_error_code == "SEND_ERROR"
