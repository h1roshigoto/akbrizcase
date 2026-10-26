"""MVP-018: real PostgreSQL locks, staff boundary, atomic audit and identity."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier, Event
from time import monotonic
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.engine import Connection
from test_order_reads import ORIGIN, business_rows, create, login
from test_order_reads import read_client as read_client

from app.core.csrf import SESSION_COOKIE
from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff_models import StaffSession, StaffUser
from app.core.staff_sessions import resolve_session
from app.modules.notify.models import EmailOutbox
from app.modules.notify.outbox import claim_due, finish_failure, finish_success
from app.modules.notify.retry_service import retry_failed_notification
from app.modules.orders.service import OrderRejected

pytestmark = pytest.mark.integration


def audits(database: Database) -> list[Any]:
    with database.unit_of_work() as session:
        return list(
            session.scalars(
                text(
                    "SELECT row_to_json(t) FROM app.audit_events t "
                    "WHERE action='notification.retry' ORDER BY at,id"
                )
            )
        )


def failed(client: TestClient, database: Database, kind: str = "commercial") -> UUID:
    identifier = create(client, database, kind)
    with database.unit_of_work() as session:
        session.execute(
            update(EmailOutbox).values(
                status="failed",
                attempts=5,
                last_error_code="SMTP_REJECTED",
                next_attempt_at=utc_now() + timedelta(hours=12),
            )
        )
    return identifier


def send(client: TestClient, identifier: UUID | str, csrf: str) -> Any:
    return client.post(
        f"/admin/orders/{identifier}/retry-notification",
        data={"csrf_token": csrf},
        headers={"Origin": ORIGIN},
    )


def unchanged(database: Database, before: Any, audit: Any) -> None:
    assert business_rows(database) == before, "Business rows changed on rejection"
    assert audits(database) == audit, "Successful retry audit appeared on rejection"


@pytest.mark.parametrize("kind", ["commercial", "support"])
@pytest.mark.parametrize("role", ["manager", "admin"])
def test_success_identity_business_rows_worker_and_double_click(
    read_client: TestClient,
    staff_database: Database,
    kind: str,
    role: str,
) -> None:
    identifier = failed(read_client, staff_database, kind)
    csrf = login(read_client, role)
    before = business_rows(staff_database)
    with staff_database.unit_of_work() as session:
        actor = resolve_session(session, read_client.cookies[SESSION_COOKIE])
        assert actor is not None
        actor_id = actor.user.id
        assert claim_due(session) is None  # failed is never automatically claimed
        start = session.scalar(select(text("clock_timestamp()")))
    page = read_client.get(f"/admin/orders/{identifier}")
    assert "Повторить уведомление" in page.text
    response = send(read_client, identifier, csrf)
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/orders/{identifier}"
    assert response.headers["cache-control"] == "no-store"
    after = business_rows(staff_database)
    row, old = after["email_outbox"][0], before["email_outbox"][0]
    assert row["status"] == "pending" and row["attempts"] == 0
    assert all(
        row[key] is None
        for key in ("lease_token", "lease_until", "sent_at", "last_error_code")
    )
    with staff_database.unit_of_work() as session:
        current = session.get(EmailOutbox, UUID(row["id"]))
        assert (
            current is not None
            and start
            <= current.next_attempt_at
            <= session.scalar(select(text("clock_timestamp()")))
        )
    clean = deepcopy(after)
    clean["email_outbox"] = before["email_outbox"]
    assert clean == before, "Original business rows changed"
    assert {k: row[k] for k in ("id", "order_id", "event_kind", "created_at")} == {
        k: old[k] for k in ("id", "order_id", "event_kind", "created_at")
    }
    audit = audits(staff_database)
    assert len(audit) == 1
    assert {
        k: audit[0][k]
        for k in ("actor_id", "entity_type", "entity_id", "result", "metadata")
    } == {
        "actor_id": str(actor_id),
        "entity_type": "email_outbox",
        "entity_id": old["id"],
        "result": "success",
        "metadata": {"from_status": "failed", "to_status": "pending"},
    }
    assert (
        "Повторить уведомление"
        not in read_client.get(f"/admin/orders/{identifier}").text
    )
    assert send(read_client, identifier, csrf).status_code == 409
    unchanged(staff_database, after, audit)
    with staff_database.unit_of_work() as session:
        claim = claim_due(session)
        assert claim is not None and claim.id == UUID(old["id"]) and claim.attempts == 1
    assert send(read_client, identifier, csrf).status_code == 409
    with staff_database.unit_of_work() as session:
        from dataclasses import replace

        stale = replace(claim, token=uuid4())
        assert not finish_success(session, stale)
        assert not finish_failure(session, stale, "SMTP_TIMEOUT")
        assert finish_success(session, claim)
    assert send(read_client, identifier, csrf).status_code == 409
    assert len(audits(staff_database)) == 1
    assert len(business_rows(staff_database)["email_outbox"]) == 1


@pytest.mark.parametrize(
    "state", ["pending", "sending", "expired-sending", "sent", "missing"]
)
def test_state_matrix(
    read_client: TestClient, staff_database: Database, state: str
) -> None:
    identifier = failed(read_client, staff_database)
    csrf = login(read_client)
    with staff_database.unit_of_work() as session:
        if state == "missing":
            session.execute(
                delete(EmailOutbox)
            )  # legitimate cleanup must not recreate it
        else:
            session.execute(
                update(EmailOutbox).values(
                    status="sending" if state == "expired-sending" else state,
                    lease_token=uuid4() if "sending" in state else None,
                    lease_until=utc_now()
                    + timedelta(seconds=-1 if state == "expired-sending" else 120)
                    if "sending" in state
                    else None,
                    sent_at=utc_now() if state == "sent" else None,
                )
            )
    before, audit = business_rows(staff_database), audits(staff_database)
    response = send(read_client, identifier, csrf)
    assert response.status_code == (404 if state == "missing" else 409)
    if state != "missing":
        assert "reload current order" in response.text
    unchanged(staff_database, before, audit)
    page = read_client.get(f"/admin/orders/{identifier}")
    assert "Повторить уведомление" not in page.text


@pytest.mark.parametrize(
    "mode",
    [
        "anonymous",
        "forged",
        "expired",
        "revoked",
        "disabled",
        "no_csrf",
        "forged_csrf",
        "other_csrf",
        "no_origin",
        "foreign_origin",
    ],
)
def test_security(read_client: TestClient, staff_database: Database, mode: str) -> None:
    identifier = failed(read_client, staff_database)
    other = login(read_client, "admin")
    csrf = login(read_client)
    if mode == "anonymous":
        read_client.cookies.clear()
    elif mode == "forged":
        read_client.cookies.clear()
        read_client.cookies.set(SESSION_COOKIE, "f" * 64)
    elif mode in ("expired", "revoked", "disabled"):
        with staff_database.unit_of_work() as session:
            if mode == "disabled":
                session.execute(
                    update(StaffUser)
                    .where(StaffUser.role == "manager")
                    .values(is_active=False)
                )
            else:
                session.execute(
                    update(StaffSession).values(
                        **{
                            "expires_at"
                            if mode == "expired"
                            else "revoked_at": utc_now() - timedelta(seconds=1)
                        }
                    )
                )
    csrf = {"no_csrf": "", "forged_csrf": "forged", "other_csrf": other}.get(mode, csrf)
    headers = (
        {}
        if mode == "no_origin"
        else {
            "Origin": "https://foreign.invalid" if mode == "foreign_origin" else ORIGIN
        }
    )
    before, audit = business_rows(staff_database), audits(staff_database)
    response = read_client.post(
        f"/admin/orders/{identifier}/retry-notification",
        data={"csrf_token": csrf},
        headers=headers,
    )
    assert response.status_code == (
        401
        if mode in ("anonymous", "forged", "expired", "revoked", "disabled")
        else 403
    )
    unchanged(staff_database, before, audit)


@pytest.mark.parametrize(
    "mode",
    [
        "uuid",
        "unknown",
        "get",
        "json",
        "empty",
        "duplicate",
        "actor_id",
        "outbox_id",
        "recipient",
        "status",
        "attempts",
        "lease_token",
        "bad_form_token",
        "query",
    ],
)
def test_strict_request(
    read_client: TestClient, staff_database: Database, mode: str
) -> None:
    identifier: UUID | str = failed(read_client, staff_database)
    csrf = login(read_client)
    before, audit = business_rows(staff_database), audits(staff_database)
    if mode == "uuid":
        identifier = "malformed"
    elif mode == "unknown":
        identifier = uuid4()
    url = f"/admin/orders/{identifier}/retry-notification"
    if mode == "query":
        url += "?actor_id=untrusted"
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    if mode == "get":
        response = read_client.get(url)
    elif mode == "json":
        response = read_client.post(url, json={"csrf_token": csrf}, headers=headers)
    else:
        fields = [("csrf_token", "forged" if mode == "bad_form_token" else csrf)]
        if mode == "empty":
            fields = []
        elif mode == "duplicate":
            fields.append(("csrf_token", csrf))
        elif mode in (
            "actor_id",
            "outbox_id",
            "recipient",
            "status",
            "attempts",
            "lease_token",
        ):
            fields.append((mode, "untrusted"))
        response = read_client.post(
            url,
            content=urlencode(fields),
            headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == {"get": 405, "unknown": 404}.get(mode, 422)
    assert response.headers["cache-control"] == "no-store"
    unchanged(staff_database, before, audit)


@pytest.mark.parametrize("failure", ["audit", "commit"])
def test_atomic_database_failure(
    read_client: TestClient, staff_database: Database, failure: str
) -> None:
    identifier = failed(read_client, staff_database)
    csrf = login(read_client)
    before, audit = business_rows(staff_database), audits(staff_database)
    hit = []

    def fail_insert(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> tuple[str, Any]:
        if statement.startswith("INSERT INTO app.audit_events"):
            hit.append(True)
            return "SELECT 1/0", {}
        return statement, parameters

    def fail_commit(connection: Connection) -> None:
        hit.append(True)
        connection.exec_driver_sql("SELECT 1/0")

    hook, callback = (
        ("before_cursor_execute", fail_insert)
        if failure == "audit"
        else ("commit", fail_commit)
    )
    event.listen(
        staff_database.engine,
        hook,
        callback,
        **({"retval": True} if failure == "audit" else {}),
    )
    try:
        assert send(read_client, identifier, csrf).status_code == 503
    finally:
        event.remove(staff_database.engine, hook, callback)
    assert hit == [True]
    unchanged(staff_database, before, audit)


def test_service_rechecks_active_role(
    read_client: TestClient, staff_database: Database
) -> None:
    identifier = failed(read_client, staff_database)
    login(read_client)
    with staff_database.unit_of_work() as session:
        actor = resolve_session(session, read_client.cookies[SESSION_COOKIE])
        assert actor is not None
        for role, active in [("manager", False), ("reader", True)]:
            actor.user.role, actor.user.is_active = role, active
            with pytest.raises(OrderRejected) as error:
                retry_failed_notification(session, identifier, actor)
            assert error.value.status == 403
        session.rollback()
    assert audits(staff_database) == []


def test_two_connections_lock_and_stale_identity_refresh(
    read_client: TestClient, staff_database: Database
) -> None:
    identifier = failed(read_client, staff_database)
    login(read_client)
    with staff_database.unit_of_work() as session:
        actor = resolve_session(session, read_client.cookies[SESSION_COOKIE])
        assert actor is not None
    before = business_rows(staff_database)
    barrier, locked, observed = Barrier(2), Event(), Event()
    pids: set[int] = set()

    def attempt(first: bool) -> int:
        with staff_database.unit_of_work() as session:
            pids.add(session.scalar(text("SELECT pg_backend_pid()")))
            loaded = session.scalar(
                select(EmailOutbox).where(EmailOutbox.order_id == identifier)
            )
            assert loaded is not None and loaded.status == "failed"
            barrier.wait(timeout=5)
            if first:
                session.execute(
                    select(EmailOutbox.id)
                    .where(EmailOutbox.order_id == identifier)
                    .with_for_update()
                )
                locked.set()
            else:
                assert locked.wait(timeout=5)
            try:
                retry_failed_notification(session, identifier, actor)
                assert loaded.status == "pending"
                assert loaded.next_attempt_at > session.scalar(
                    text("SELECT transaction_timestamp()")
                )
                if first:
                    deadline = monotonic() + 2
                    while monotonic() < deadline:
                        if session.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE pid <> pg_backend_pid() "
                                "AND usename=current_user AND wait_event_type='Lock' "
                                "AND query LIKE '%FOR UPDATE%'"
                            )
                        ):
                            observed.set()
                            break
                        session.execute(text("SELECT pg_stat_clear_snapshot()"))
                        observed.wait(0.01)
                    assert observed.is_set(), (
                        "Independent connection never waited on row lock"
                    )
            except OrderRejected as error:
                return error.status
            return 303

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, first) for first in (True, False)]
        results = [future.result(timeout=10) for future in futures]
    assert len(pids) == 2 and observed.is_set()
    assert sorted(results) == [303, 409]
    after = business_rows(staff_database)
    assert len(after["email_outbox"]) == 1
    assert after["email_outbox"][0]["id"] == before["email_outbox"][0]["id"]
    assert after["email_outbox"][0]["attempts"] == 0
    assert len(audits(staff_database)) == 1


def test_concurrent_http(read_client: TestClient, staff_database: Database) -> None:
    identifier = failed(read_client, staff_database)
    identities = []
    for _ in range(2):
        csrf = login(read_client, "manager")
        identities.append((csrf, read_client.cookies[SESSION_COOKIE]))
    read_client.cookies.clear()
    barrier = Barrier(2)

    def attempt(identity: tuple[str, str]) -> int:
        csrf, token = identity
        barrier.wait(timeout=5)
        return read_client.post(
            f"/admin/orders/{identifier}/retry-notification",
            data={"csrf_token": csrf},
            headers={"Origin": ORIGIN, "Cookie": f"{SESSION_COOKIE}={token}"},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, identities)) == [303, 409]
    assert len(audits(staff_database)) == 1
    row = business_rows(staff_database)["email_outbox"]
    assert len(row) == 1 and row[0]["attempts"] == 0


def test_original_replay_and_aq01_after_retry(
    read_client: TestClient,
    staff_database: Database,
) -> None:
    from app.modules.orders.models import Order

    captured: dict[str, Any] = {}
    original_post = read_client.post

    def capture(url: str, **kwargs: Any) -> Any:
        if url == "/api/v1/orders":
            captured.update(kwargs)
        return original_post(url, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(read_client, "post", capture)
        identifier = failed(read_client, staff_database)
    receipt = original_post("/api/v1/orders", **captured).json()
    csrf = login(read_client)
    assert send(read_client, identifier, csrf).status_code == 303
    before, audit = business_rows(staff_database), audits(staff_database)
    replay = original_post("/api/v1/orders", **captured)
    assert replay.status_code == 200 and replay.json() == receipt
    unchanged(staff_database, before, audit)
    with staff_database.unit_of_work() as session:
        session.execute(
            update(Order)
            .where(Order.id == identifier)
            .values(
                pii_erased_at=utc_now(),
                contact_name=None,
                email=None,
                phone=None,
                organization_name=None,
                city=None,
                comment=None,
                request_hash=None,
                receipt_token_hash=None,
            )
        )
        session.execute(delete(EmailOutbox).where(EmailOutbox.order_id == identifier))
    before, audit = business_rows(staff_database), audits(staff_database)
    assert original_post("/api/v1/orders", **captured).status_code == 404
    assert send(read_client, identifier, csrf).status_code == 404
    unchanged(staff_database, before, audit)
