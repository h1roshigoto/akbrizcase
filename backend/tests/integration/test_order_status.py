"""Real PostgreSQL status transactions, security, races and immutable receipts."""

import html
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from threading import Barrier, Event
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text, update
from sqlalchemy.engine import Connection
from test_order_reads import (
    ATTACK,
    ORIGIN,
    business_rows,
    create,
    login,
)
from test_order_reads import (
    read_client as read_client,
)

from app.core.csrf import SESSION_COOKIE
from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff import lock_staff
from app.core.staff_models import StaffSession, StaffUser
from app.core.staff_sessions import resolve_session
from app.modules.orders.models import Order
from app.modules.orders.service import OrderRejected
from app.modules.orders.status_service import change_status
from app.schemas.admin import OrderStatusChange

pytestmark = pytest.mark.integration
REASON = ATTACK + "\nSynthetic processing complete"


@pytest.fixture
def status_client(read_client: TestClient) -> TestClient:
    return read_client


def audits(database: Database) -> list[dict[str, Any]]:
    with database.unit_of_work() as session:
        return list(
            session.scalars(
                text(
                    "SELECT row_to_json(t) FROM app.audit_events t "
                    "WHERE action='order.status.change' ORDER BY at,id"
                )
            )
        )


def send(
    client: TestClient,
    identifier: UUID | str,
    csrf: str,
    body: dict[str, Any],
    *,
    form: bool = False,
) -> Any:
    if form:
        return client.post(
            f"/admin/orders/{identifier}/status",
            data={"csrf_token": csrf, **body},
            headers={"Origin": ORIGIN},
        )
    return client.patch(
        f"/api/v1/admin/orders/{identifier}/status",
        json=body,
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )


def unchanged(
    database: Database, before: dict[str, Any], before_audit: list[dict[str, Any]]
) -> None:
    # Keep private rows/hashes out of assertion diagnostics.
    assert business_rows(database) == before, "Business rows changed on rejection"
    assert audits(database) == before_audit, "Successful audit appeared on rejection"


@pytest.mark.parametrize("kind", ["commercial", "support"])
@pytest.mark.parametrize("role", ["manager", "admin"])
@pytest.mark.parametrize(
    "path",
    [
        ["in_progress", "closed"],
        ["cancelled"],
        ["in_progress", "cancelled"],
    ],
)
def test_successful_paths_and_full_invariants(
    status_client: TestClient,
    staff_database: Database,
    kind: str,
    role: str,
    path: list[str],
) -> None:
    identifier = create(status_client, staff_database, kind)
    csrf = login(status_client, role)
    original = business_rows(staff_database)
    with staff_database.unit_of_work() as session:
        actor = session.scalar(
            select(StaffUser.id).where(StaffUser.login == f"read-{role}")
        )
        # Current catalog prices never participate in status changes.
        session.execute(
            text(
                "UPDATE app.catalog_items SET revision=revision+1, "
                "name='Synthetic changed catalog'"
            )
        )
    previous = "new"
    for version, target in enumerate(path, start=2):
        start = utc_now()
        response = send(
            status_client,
            identifier,
            csrf,
            {
                "status": target,
                "expected_version": version - 1,
                "reason": REASON,
            },
        )
        assert response.status_code == 200
        assert response.json() == {"status": target, "version": version}
        assert response.headers["cache-control"] == "no-store"
        after = business_rows(staff_database)
        order = after["orders"][0]
        assert (order["status"], order["version"]) == (target, version)
        history = sorted(after["order_status_history"], key=lambda row: row["at"])
        assert len(history) == version
        assert history[0] == original["order_status_history"][0]
        last = history[-1]
        assert (last["from_status"], last["to_status"], last["reason"]) == (
            previous,
            target,
            REASON,
        )
        assert last["changed_by"] == str(actor)
        assert order["updated_at"] == last["at"]
        assert start <= datetime.fromisoformat(last["at"]) <= utc_now()
        assert order["closed_at"] == (last["at"] if target == "closed" else None)
        event_rows = audits(staff_database)
        assert len(event_rows) == version - 1
        audit = event_rows[-1]
        assert (
            audit["entity_type"],
            audit["entity_id"],
            audit["actor_id"],
            audit["result"],
        ) == (
            "order",
            str(identifier),
            str(actor),
            "success",
        )
        assert audit["at"] == last["at"]
        assert audit["metadata"] == {
            "from_status": previous,
            "to_status": target,
            "from_version": version - 1,
            "to_version": version,
        }
        clean = deepcopy(after)
        clean["order_status_history"] = original["order_status_history"]
        for key in ("status", "version", "updated_at", "closed_at"):
            clean["orders"][0][key] = original["orders"][0][key]
        assert clean == original, (
            "Immutable order, consent, snapshots or outbox changed"
        )
        previous = target
    if kind == "support":
        assert after["order_items"] == []
        assert after["orders"][0]["comment"] == ATTACK


@pytest.mark.parametrize("kind", ["commercial", "support"])
@pytest.mark.parametrize(
    "source,target",
    [
        ("new", "closed"),
        ("in_progress", "in_progress"),
        ("closed", "in_progress"),
        ("closed", "closed"),
        ("closed", "cancelled"),
        ("cancelled", "in_progress"),
        ("cancelled", "closed"),
        ("cancelled", "cancelled"),
    ],
)
def test_disallowed_matrix(
    status_client: TestClient,
    staff_database: Database,
    kind: str,
    source: str,
    target: str,
) -> None:
    identifier = create(status_client, staff_database, kind)
    csrf = login(status_client)
    version = 1
    for intermediate in {
        "new": [],
        "in_progress": ["in_progress"],
        "closed": ["in_progress", "closed"],
        "cancelled": ["cancelled"],
    }[source]:
        assert (
            send(
                status_client,
                identifier,
                csrf,
                {
                    "status": intermediate,
                    "expected_version": version,
                    "reason": REASON,
                },
            ).status_code
            == 200
        )
        version += 1
    before, audit = business_rows(staff_database), audits(staff_database)
    assert (
        send(
            status_client,
            identifier,
            csrf,
            {
                "status": target,
                "expected_version": version,
                "reason": REASON,
            },
        ).status_code
        == 409
    )
    unchanged(staff_database, before, audit)


@pytest.mark.parametrize("form", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        {"status": "in_progress"},
        {"status": "in_progress", "expected_version": 0},
        {"status": "in_progress", "expected_version": -1},
        {"status": "closed", "expected_version": 1},
        *[
            {"status": terminal, "expected_version": 1, "reason": reason}
            for terminal in ("closed", "cancelled")
            for reason in (None, "", " \t\n\u2003", "x" * 2001)
        ],
        {"status": "unknown", "expected_version": 1},
        {"status": "new", "expected_version": 1},
        *[
            {"status": "in_progress", "expected_version": 1, field: "spoofed"}
            for field in ("changed_by", "actor_id", "version", "closed_at", "comment")
        ],
    ],
)
def test_invalid_inputs(
    status_client: TestClient,
    staff_database: Database,
    form: bool,
    body: dict[str, Any],
) -> None:
    identifier = create(status_client, staff_database)
    csrf = login(status_client)
    before, audit = business_rows(staff_database), audits(staff_database)
    assert send(status_client, identifier, csrf, body, form=form).status_code == 422
    unchanged(staff_database, before, audit)


@pytest.mark.parametrize("value", [True, 1.0, "1", None])
def test_strict_json_version(
    status_client: TestClient, staff_database: Database, value: Any
) -> None:
    identifier = create(status_client, staff_database)
    csrf = login(status_client)
    before, audit = business_rows(staff_database), audits(staff_database)
    assert (
        send(
            status_client,
            identifier,
            csrf,
            {"status": "in_progress", "expected_version": value},
        ).status_code
        == 422
    )
    unchanged(staff_database, before, audit)


@pytest.mark.parametrize("form", [False, True])
def test_missing_invalid_stale_and_html(
    status_client: TestClient, staff_database: Database, form: bool
) -> None:
    identifier = create(status_client, staff_database)
    csrf = login(status_client)
    before, audit = business_rows(staff_database), audits(staff_database)
    for identity, version, code in [
        (str(uuid4()), 1, 404),
        ("bad-uuid", 1, 422),
        (str(identifier), 9, 409),
    ]:
        response = send(
            status_client,
            identity,
            csrf,
            {"status": "in_progress", "expected_version": version},
            form=form,
        )
        assert response.status_code == code
        unchanged(staff_database, before, audit)
    response = send(
        status_client,
        identifier,
        csrf,
        {"status": "in_progress", "expected_version": 1, "reason": REASON},
        form=form,
    )
    assert response.status_code == (303 if form else 200)
    if form:
        assert response.headers["location"] == f"/admin/orders/{identifier}"
    before, audit = business_rows(staff_database), audits(staff_database)
    response = send(
        status_client,
        identifier,
        csrf,
        {"status": "cancelled", "expected_version": 1, "reason": REASON},
        form=form,
    )
    assert response.status_code == 409
    if form:
        assert "Reload current order" in response.text
    unchanged(staff_database, before, audit)
    page = status_client.get(f"/admin/orders/{identifier}")
    assert REASON in html.unescape(page.text)
    assert "<script>window.orderInjected" not in page.text
    assert 'value="2"' in page.text
    assert send(
        status_client,
        identifier,
        csrf,
        {"status": "closed", "expected_version": 2, "reason": REASON},
        form=form,
    ).status_code == (303 if form else 200)
    page = status_client.get(f"/admin/orders/{identifier}")
    assert 'name="expected_version"' not in page.text
    assert "No further transitions" in page.text


@pytest.mark.parametrize("form", [False, True])
@pytest.mark.parametrize(
    "mode",
    [
        "missing",
        "forged",
        "other_csrf",
        "no_csrf",
        "origin",
        "expired",
        "revoked",
        "disabled",
    ],
)
def test_staff_boundary(
    status_client: TestClient, staff_database: Database, form: bool, mode: str
) -> None:
    identifier = create(status_client, staff_database)
    other_csrf = login(status_client, "admin")
    csrf = login(status_client)
    if mode == "missing":
        status_client.cookies.clear()
    elif mode == "forged":
        status_client.cookies.clear()
        status_client.cookies.set(SESSION_COOKIE, "e" * 64)
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
                            else "revoked_at": utc_now() - timedelta(seconds=1),
                        }
                    )
                )
    if mode == "no_csrf":
        csrf = ""
    elif mode == "other_csrf":
        csrf = other_csrf
    before, audit = business_rows(staff_database), audits(staff_database)
    body = {"status": "in_progress", "expected_version": 1}
    if mode == "origin":
        response = status_client.request(
            "POST" if form else "PATCH",
            f"{'/admin' if form else '/api/v1/admin'}/orders/{identifier}/status",
            json=body,
            headers={"Origin": "https://foreign.invalid", "X-CSRF-Token": csrf},
        )
    else:
        response = send(status_client, identifier, csrf, body, form=form)
    assert response.status_code == (
        403 if mode in ("other_csrf", "no_csrf", "origin") else 401
    )
    unchanged(staff_database, before, audit)


def test_concurrent_row_lock_and_refresh(
    status_client: TestClient, staff_database: Database
) -> None:
    identifier = create(status_client, staff_database)
    login(status_client)
    token = status_client.cookies.get(SESSION_COOKIE)
    assert token is not None
    with staff_database.unit_of_work() as session:
        lock_staff(session)
        actor = resolve_session(session, token)
        assert actor is not None
    barrier = Barrier(2)
    locked = Event()
    observed_wait = Event()
    pids: set[int] = set()

    def attempt(first: bool) -> int:
        with staff_database.unit_of_work() as session:
            pid = session.scalar(text("SELECT pg_backend_pid()"))
            assert pid is not None
            pids.add(pid)
            # Preload version 1 in both identity maps; service must refresh it.
            loaded = session.get(Order, identifier)
            assert loaded is not None and loaded.version == 1
            barrier.wait(timeout=5)
            if first:
                session.execute(
                    select(Order.id).where(Order.id == identifier).with_for_update()
                )
                locked.set()
            else:
                assert locked.wait(timeout=5)
            try:
                change_status(
                    session,
                    identifier,
                    OrderStatusChange(status="in_progress", expected_version=1),
                    actor,
                )
                if first:
                    # Prove the other independent connection really waits on
                    # PostgreSQL's row lock before allowing the winner to commit.
                    deadline = monotonic() + 2
                    while monotonic() < deadline:
                        waiting = session.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE pid <> pg_backend_pid() "
                                "AND usename=current_user AND wait_event_type='Lock' "
                                "AND query LIKE '%FOR UPDATE%'"
                            )
                        )
                        if waiting:
                            observed_wait.set()
                            break
                        session.execute(text("SELECT pg_stat_clear_snapshot()"))
                        observed_wait.wait(0.01)
                    assert observed_wait.is_set(), (
                        "Independent writer never waited on row lock"
                    )
            except OrderRejected as error:
                return error.status
            return 200

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, first) for first in (True, False)]
        results = [future.result(timeout=10) for future in futures]
    assert len(pids) == 2
    assert observed_wait.is_set()
    assert sorted(results) == [200, 409]
    rows = business_rows(staff_database)
    assert rows["orders"][0]["version"] == 2
    assert len(rows["order_status_history"]) == 2
    assert len(audits(staff_database)) == 1


@pytest.mark.parametrize("table", ["order_status_history", "audit_events"])
@pytest.mark.parametrize("form", [False, True])
def test_real_database_insert_failure_rolls_back(
    status_client: TestClient, staff_database: Database, table: str, form: bool
) -> None:
    identifier = create(status_client, staff_database)
    csrf = login(status_client)
    before, audit = business_rows(staff_database), audits(staff_database)
    hit = []

    def fail(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> tuple[str, Any]:
        if statement.startswith(f"INSERT INTO app.{table}"):
            hit.append(table)
            # A real PostgreSQL error after preceding writes, no mock session.
            return "SELECT 1/0", {}
        return statement, parameters

    event.listen(staff_database.engine, "before_cursor_execute", fail, retval=True)
    try:
        assert (
            send(
                status_client,
                identifier,
                csrf,
                {"status": "in_progress", "expected_version": 1},
                form=form,
            ).status_code
            == 503
        )
    finally:
        event.remove(staff_database.engine, "before_cursor_execute", fail)
    assert hit == [table]
    unchanged(staff_database, before, audit)


def test_replay_after_close_is_original_receipt(
    status_client: TestClient, staff_database: Database
) -> None:
    # Capture exact real creation request privately, then replay after processing.
    captured: dict[str, Any] = {}
    original_post = status_client.post

    def capture(url: str, **kwargs: Any) -> Any:
        if url == "/api/v1/orders":
            captured.update(kwargs)
        return original_post(url, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(status_client, "post", capture)
        identifier = create(status_client, staff_database)
    receipt = original_post("/api/v1/orders", **captured).json()
    csrf = login(status_client)
    for version, status in enumerate(("in_progress", "closed"), start=1):
        assert (
            send(
                status_client,
                identifier,
                csrf,
                {"status": status, "expected_version": version, "reason": REASON},
            ).status_code
            == 200
        )
    before, audit = business_rows(staff_database), audits(staff_database)
    replay = original_post("/api/v1/orders", **captured)
    assert replay.status_code == 200 and replay.json() == receipt
    assert receipt["status"] == "new"
    unchanged(staff_database, before, audit)
    with staff_database.unit_of_work() as session:
        # Existing AQ-01 erasure contract, in this isolated synthetic DB only.
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
    erased = original_post("/api/v1/orders", **captured)
    assert erased.status_code == 404 and erased.json()["error"]["code"] == "NOT_FOUND"
    assert REASON not in json.dumps(audit)


@pytest.mark.parametrize("form", [False, True])
def test_outer_commit_failure_cannot_return_success(
    status_client: TestClient,
    staff_database: Database,
    form: bool,
) -> None:
    identifier = create(status_client, staff_database)
    csrf = login(status_client)
    before, audit = business_rows(staff_database), audits(staff_database)

    def fail_commit(connection: Connection) -> None:
        connection.exec_driver_sql("SELECT 1/0")

    event.listen(staff_database.engine, "commit", fail_commit)
    try:
        response = send(
            status_client,
            identifier,
            csrf,
            {"status": "in_progress", "expected_version": 1},
            form=form,
        )
        assert response.status_code == 503
    finally:
        event.remove(staff_database.engine, "commit", fail_commit)
    unchanged(staff_database, before, audit)


def test_duplicate_form_fields_and_json_actor_spoof(
    status_client: TestClient,
    staff_database: Database,
) -> None:
    identifier = create(status_client, staff_database)
    csrf = login(status_client)
    before, audit = business_rows(staff_database), audits(staff_database)
    from urllib.parse import urlencode

    response = status_client.post(
        f"/admin/orders/{identifier}/status",
        content=urlencode(
            [
                ("csrf_token", csrf),
                ("status", "in_progress"),
                ("expected_version", "1"),
                ("expected_version", "2"),
            ]
        ),
        headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422
    unchanged(staff_database, before, audit)
    status_client.cookies.clear()
    response = status_client.patch(
        f"/api/v1/admin/orders/{identifier}/status",
        json={"status": "in_progress", "expected_version": 1},
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "X-Staff-Role": "admin",
            "X-Actor-ID": str(uuid4()),
        },
    )
    assert response.status_code == 401
    unchanged(staff_database, before, audit)


def test_two_http_managers_one_success_one_conflict(
    status_client: TestClient,
    staff_database: Database,
) -> None:
    identifier = create(status_client, staff_database)
    identities = []
    for role in ("manager", "admin"):
        csrf = login(status_client, role)
        token = status_client.cookies.get(SESSION_COOKIE)
        assert token is not None
        identities.append((csrf, token))
    status_client.cookies.clear()
    barrier = Barrier(2)

    def attempt(identity: tuple[str, str]) -> int:
        csrf, token = identity
        barrier.wait(timeout=5)
        response = status_client.patch(
            f"/api/v1/admin/orders/{identifier}/status",
            json={"status": "in_progress", "expected_version": 1},
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf,
                "Cookie": f"{SESSION_COOKIE}={token}",
            },
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, identities))
    assert sorted(results) == [200, 409]
    rows = business_rows(staff_database)
    assert rows["orders"][0]["version"] == 2
    assert len(rows["order_status_history"]) == 2
    assert len(audits(staff_database)) == 1


def test_reason_maximum_preserved_without_normalization(
    status_client: TestClient,
    staff_database: Database,
) -> None:
    identifier = create(status_client, staff_database, "support")
    csrf = login(status_client)
    reason = "  " + "\u042f" * 1996 + "\n "
    assert len(reason) == 2000
    assert (
        send(
            status_client,
            identifier,
            csrf,
            {
                "status": "cancelled",
                "expected_version": 1,
                "reason": reason,
            },
        ).status_code
        == 200
    )
    history = business_rows(staff_database)["order_status_history"]
    assert (
        next(row for row in history if row["from_status"] is not None)["reason"]
        == reason
    )
