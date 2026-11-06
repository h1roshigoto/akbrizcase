"""MVP-044: connected commercial lifecycles, PG faults and real contention.

Each test starts with HTTP-created synthetic orders. Existing per-module suites
remain separate; only their fixtures/helpers, never their test functions, are reused.
"""

import re
import secrets
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.parser import Parser
from pathlib import Path
from threading import Barrier, Event
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from commercial_helpers import Graph, graph, pg_fault, same, wait_for_blocker
from fastapi.testclient import TestClient
from smtp_double import Sink
from sqlalchemy import Table, delete, event, func, insert, literal, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session
from test_email_worker import configuration
from test_email_worker import sink as sink
from test_order_creation import commercial, order_headers, support_body
from test_privacy import PASSWORD, packet, publish
from test_privacy import privacy_db as privacy_db

from app.core.csrf import SESSION_COOKIE, StaffSecurity
from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff_models import StaffUser
from app.core.staff_sessions import login_staff
from app.main import app
from app.modules.catalog.models import CatalogItem, EquipmentModel
from app.modules.catalog.seed import seed_catalog
from app.modules.catalog.write_dto import ItemEdit, ItemFields, ItemState
from app.modules.catalog.write_service import CatalogWriteService
from app.modules.chat.models import ChatSession
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
from app.modules.notify.smtp import send_notification
from app.modules.orders.cleanup_service import (
    CleanupRow,
    apply_command,
    eligibility,
    prepare_entry,
)
from app.modules.orders.deletion_journal import (
    DeletionCommand,
    JournalUnavailable,
    SyntheticJournal,
    command_for,
)
from app.modules.orders.deletion_operator import authorize, prepare_deletion
from app.modules.orders.deletion_reconciliation import JournalState, reconcile_command
from app.modules.orders.models import (
    Consent,
    ConsentText,
    Order,
    OrderItem,
    OrderStatusHistory,
)
from app.modules.orders.privacy_operator import run_privacy
from app.modules.orders.security import OrderSecurity
from app.modules.orders.seed import DEMO_VERSION, demo_bundle
from app.worker.runner import tick

pytestmark = pytest.mark.integration
ORIGIN = "https://localhost:8443"


@pytest.fixture
def commercial_db(privacy_db: tuple[Database, Database]) -> Iterator[Database]:
    runtime, migrator = privacy_db
    with migrator.unit_of_work() as session:
        session.execute(text("TRUNCATE app.deletion_journal, app.cleanup_cursors"))
    seed_catalog(runtime, mode="test")
    # Use the permitted service, including its authority/revision/audit, to
    # prepare the fixed-product/on-request-service acceptance fixture.
    with runtime.unit_of_work() as session:
        issued = login_staff(session, "privacy-admin", PASSWORD)
        assert issued is not None
        row = session.scalars(
            select(CatalogItem).where(CatalogItem.sku == "DEMO-S-001")
        ).one()
        values = {name: getattr(row, name) for name in ItemFields.model_fields}
        CatalogWriteService(session, issued.identity).edit_item(
            row.id,
            ItemEdit.model_validate(
                values
                | {
                    "expected_revision": row.revision,
                    "price_mode": "on_request",
                    "price": None,
                }
            ),
        )
    try:
        yield runtime
    finally:
        with migrator.unit_of_work() as session:
            session.execute(delete(Order))
            session.execute(delete(Consent))
            session.execute(delete(ChatSession))
            session.execute(delete(CatalogItem))
            session.execute(delete(EquipmentModel))


@pytest.fixture
def commercial_client(commercial_db: Database) -> Iterator[TestClient]:
    with TestClient(
        app, base_url=ORIGIN, client=("192.0.2.44", 50000), follow_redirects=False
    ) as client:
        app.state.database = commercial_db
        app.state.order_security = OrderSecurity(secrets.token_bytes(32))
        app.state.staff_security = StaffSecurity(
            frozenset({ORIGIN}), secrets.token_hex(32)
        )
        yield client


@dataclass(repr=False)
class Accepted:
    id: UUID
    body: dict[str, Any] = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    receipt: dict[str, Any]


def accept(
    client: TestClient,
    database: Database,
    kind: str = "mixed",
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Accepted:
    if body is None:
        body = (
            support_body()
            if kind == "support"
            else commercial(
                database,
                ("DEMO-S-001",) if kind == "service" else ("DEMO-P-001", "DEMO-S-001"),
            )
        )
    headers = order_headers() if headers is None else headers
    response = client.post("/api/v1/orders", json=body, headers=headers)
    same(response.status_code, 201, "HTTP creation")
    receipt = response.json()
    same(
        set(receipt),
        {
            "number",
            "status",
            "known_subtotal",
            "items_total",
            "delivery_status",
            "delivery_cost",
            "total_amount",
            "currency",
        },
    )
    same(receipt["status"], "new")
    same(receipt["items_total"], None)
    same(receipt["total_amount"], None)
    with database.unit_of_work() as session:
        identifier = session.scalars(
            select(Order.id).where(
                Order.idempotency_key == UUID(headers["Idempotency-Key"])
            )
        ).one()
    current = graph(database)
    order = current.order(identifier)
    same(len(current.linked(identifier, "email_outbox")), 1)
    same(current.linked(identifier, "email_outbox")[0]["status"], "pending")
    same(order["status"], "new")
    same(order["version"], 1)
    same(current.linked(identifier, "consents")[0]["text_version"], DEMO_VERSION)
    same(order["comment"], body["comment"], "confirmed comment")
    with database.unit_of_work() as session:
        for item in current.linked(identifier, "order_items"):
            catalog = session.get(CatalogItem, UUID(item["item_id"]))
            assert catalog is not None
            for snap, column in (
                ("sku", "sku"),
                ("name", "name"),
                ("kind", "kind"),
                ("unit", "unit"),
                ("price_mode", "price_mode"),
                ("unit_price", "price"),
                ("currency", "currency"),
                ("tax_note", "tax_note"),
                ("requires_shipping", "requires_shipping"),
                ("catalog_revision", "revision"),
            ):
                value = getattr(catalog, column)
                if column == "price" and value is not None:
                    value = float(value)  # row_to_json numeric representation
                same(item[snap + "_snapshot"], value, "catalog snapshot")
    return Accepted(identifier, body, headers, receipt)


def login(client: TestClient, role: str = "manager") -> str:
    client.cookies.clear()
    csrf = client.get("/admin/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/admin/auth/login",
        json={"login": f"privacy-{role}", "password": PASSWORD},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    same(response.status_code, 200, "staff login")
    return str(response.json()["csrf_token"])


def status(
    client: TestClient, order: Accepted, csrf: str, target: str, version: int
) -> Any:
    database = app.state.database
    before = graph(database)
    response = client.patch(
        f"/api/v1/admin/orders/{order.id}/status",
        json={
            "status": target,
            "expected_version": version,
            "reason": "Synthetic completion reason",
        },
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    after = graph(database)
    if response.status_code == 200:
        mutable = {"status", "version", "updated_at", "closed_at", "terminal_at"}
        same(
            {
                key: val
                for key, val in after.order(order.id).items()
                if key not in mutable
            },
            {
                key: val
                for key, val in before.order(order.id).items()
                if key not in mutable
            },
            "status immutable order fields",
        )
        for table in ("order_items", "consents", "email_outbox"):
            same(after.linked(order.id, table), before.linked(order.id, table))
        history = after.linked(order.id, "order_status_history")
        added = [
            row
            for row in history
            if row not in before.linked(order.id, "order_status_history")
        ]
        same(len(added), 1)
        events = after.audits("order.status.change", str(order.id))
        added_events = [
            row
            for row in events
            if row not in before.audits("order.status.change", str(order.id))
        ]
        same(len(added_events), 1)
        same(added[0]["changed_by"], added_events[0]["actor_id"])
        same(added[0]["at"], added_events[0]["at"])
    else:
        same(after.business(order.id), before.business(order.id), "rejected status")
    return response


def retry(client: TestClient, order: Accepted, csrf: str) -> Any:
    return client.post(
        f"/admin/orders/{order.id}/retry-notification",
        data={"csrf_token": csrf},
        headers={"Origin": ORIGIN},
    )


def replay(client: TestClient, order: Accepted) -> None:
    result = client.post("/api/v1/orders", json=order.body, headers=order.headers)
    same(result.status_code, 200, "original replay")
    same(result.json(), order.receipt, "original receipt")


def catalog_change(database: Database, *, archive: bool = True) -> None:
    with database.unit_of_work() as session:
        issued = login_staff(session, "privacy-admin", PASSWORD)
        assert issued is not None
        row = session.scalars(
            select(CatalogItem).where(CatalogItem.sku == "DEMO-P-001")
        ).one()
        service = CatalogWriteService(session, issued.identity)
        values = {name: getattr(row, name) for name in ItemFields.model_fields}
        service.edit_item(
            row.id,
            ItemEdit.model_validate(
                values
                | {
                    "expected_revision": row.revision,
                    "price": "987.65",
                    "name": "Synthetic revised product",
                    "requires_shipping": False,
                }
            ),
        )
        if archive:
            service.change_item_state(
                row.id, ItemState(expected_revision=row.revision), "archive"
            )


def due(database: Database, identifier: UUID, *, expire: bool = False) -> None:
    with database.unit_of_work() as session:
        session.execute(
            update(EmailOutbox)
            .where(EmailOutbox.order_id == identifier)
            .values(
                **{
                    ("lease_until" if expire else "next_attempt_at"): database_now(
                        session
                    )
                    - LEASE
                }
            )
        )


def prepare(
    database: Database, operation: str, identifier: UUID, now: datetime
) -> DeletionCommand:
    from app.modules.orders.cleanup_service import MODELS

    with database.unit_of_work() as session:
        row = cast(CleanupRow | None, session.get(MODELS[operation], identifier))
        assert row is not None
        bundle, eligible, _ = eligibility(session, operation, row, now)
        same(eligible, True, "policy eligibility")
        return command_for(prepare_entry(session, operation, identifier, bundle, now))


def journal(directory: Path) -> SyntheticJournal:
    directory.mkdir(mode=0o700)
    return SyntheticJournal(directory)


def terminal(
    client: TestClient, database: Database, order: Accepted, csrf: str
) -> datetime:
    current = graph(database).order(order.id)
    if current["status"] == "new":
        same(status(client, order, csrf, "in_progress", 1).status_code, 200)
    same(status(client, order, csrf, "closed", 2).status_code, 200)
    with database.unit_of_work() as session:
        row = session.get(Order, order.id)
        assert row is not None and row.terminal_at is not None
        return row.terminal_at + timedelta(days=181)


def hidden_signature(response: Any) -> tuple[object, object]:
    same(response.status_code, 404, "hidden response")
    value = response.json()
    same(set(value), {"error", "request_id"})
    same(
        value["error"],
        {"code": "NOT_FOUND", "message": "Object not found", "details": {}},
    )
    same(response.headers["cache-control"], "no-store")
    UUID(value["request_id"])
    # Security middleware creates fresh request IDs and CSP nonces per request.
    headers = {
        key: re.sub(r"'nonce-[^']+'", "'nonce-REDACTED'", val)
        for key, val in response.headers.items()
        if key != "x-request-id"
    }
    same(
        any(
            word in key
            for key in headers
            for word in ("erasure", "receipt", "owner", "token")
        ),
        False,
    )
    return value["error"], headers


@pytest.mark.parametrize("kind", ["mixed", "service", "support"])
def test_linked_lifecycle_catalog_status_smtp_retry_privacy(
    commercial_client: TestClient,
    commercial_db: Database,
    sink: Sink,
    tmp_path: Path,
    kind: str,
    privacy_db: tuple[Database, Database],
) -> None:
    client, db = commercial_client, commercial_db
    order = accept(client, db, kind)
    neighbor = accept(client, db, "support")
    # Only target's due notification is worked; a neighbor remains pending.
    with db.unit_of_work() as session:
        session.execute(
            update(EmailOutbox)
            .where(EmailOutbox.order_id == neighbor.id)
            .values(next_attempt_at=database_now(session) + timedelta(days=365))
        )
    original = graph(db)
    neighbor_before = original.business(neighbor.id)
    catalog_change(db)
    same(graph(db).business(order.id), original.business(order.id), "catalog isolation")
    stale = client.post(
        "/api/v1/orders", json=commercial(db, ("DEMO-P-001",)), headers=order_headers()
    )
    same(stale.status_code, 409)
    same(stale.json()["error"]["code"], "CATALOG_CHANGED")
    replay(client, order)
    csrf = login(client)
    same(status(client, order, csrf, "in_progress", 1).status_code, 200)
    current = graph(db)
    same(len(current.audits("order.status.change", str(order.id))), 1)
    same(status(client, order, csrf, "cancelled", 1).status_code, 409)
    same(graph(db).rows, current.rows, "stale transition")
    same(
        current.linked(order.id, "order_items"),
        original.linked(order.id, "order_items"),
    )
    same(current.linked(order.id, "consents"), original.linked(order.id, "consents"))
    replay(client, order)
    stable = graph(db).business(order.id, outbox=False)
    same(tick(db, None, send_notification), False)
    sink.mode = "reject"
    for attempt in range(1, 6):
        same(tick(db, configuration(sink), send_notification), True)
        snapshot = graph(db)
        same(snapshot.business(order.id, outbox=False), stable, "SMTP failure")
        notification = snapshot.linked(order.id, "email_outbox")[0]
        same(notification["attempts"], attempt)
        same(notification["last_error_code"], "SMTP_REJECTED")
        same(notification["status"], "failed" if attempt == 5 else "pending")
        if attempt < 5:
            with db.unit_of_work() as session:
                row = session.get(EmailOutbox, UUID(notification["id"]))
                assert row is not None
                delay = row.next_attempt_at - database_now(session)
                assert (
                    RETRY[attempt - 1] - timedelta(seconds=2)
                    <= delay
                    <= RETRY[attempt - 1]
                )
            same(tick(db, configuration(sink), send_notification), False)
            due(db, order.id)
    same(tick(db, configuration(sink), send_notification), False)
    same(retry(client, order, csrf).status_code, 303)
    pending = graph(db)
    for name in ("id", "order_id", "event_kind", "created_at"):
        same(pending.linked(order.id, "email_outbox")[0][name], notification[name])
    same(pending.linked(order.id, "email_outbox")[0]["attempts"], 0)
    same(len(pending.audits("notification.retry", notification["id"])), 1)
    same(retry(client, order, csrf).status_code, 409)
    sink.mode = "accept"
    same(tick(db, configuration(sink), send_notification), True)
    sent = graph(db)
    same(sent.business(order.id, outbox=False), stable, "SMTP success")
    same(sent.linked(order.id, "email_outbox")[0]["status"], "sent")
    same(
        str(Parser().parsestr(sink.messages[0])["Message-ID"]),
        f"<outbox.{notification['id']}@example.invalid>",
    )
    same(len(sink.messages), 1)
    same(retry(client, order, csrf).status_code, 409)
    # Retention removes the sent notification through its own witnessed command.
    sent_at = datetime.fromisoformat(
        sent.linked(order.id, "email_outbox")[0]["sent_at"]
    )
    cleanup_at = sent_at + timedelta(days=demo_bundle().retention.outbox_sent_days + 1)
    sink_journal = journal(tmp_path / "journal")
    outbox_command = prepare(db, "outbox_delete", UUID(notification["id"]), cleanup_at)
    same(
        reconcile_command(
            db, outbox_command, sink_journal, execute=True, now=cleanup_at
        ).commit_confirmed,
        True,
    )
    replay(client, order)
    same(graph(db).linked(order.id, "email_outbox"), [])
    erase_at = terminal(client, db, order, csrf)
    path, bundle = packet(tmp_path)
    same(publish(privacy_db[1], path, bundle).ok, True)
    command = prepare(db, "order_pii", order.id, erase_at)
    same(command.policy_version, DEMO_VERSION)
    same(
        reconcile_command(
            db, command, sink_journal, execute=True, now=erase_at
        ).commit_confirmed,
        True,
    )
    erased = graph(db)
    same(
        erased.linked(order.id, "order_items"), original.linked(order.id, "order_items")
    )
    same(erased.business(neighbor.id), neighbor_before, "neighbor isolation")
    client.cookies.clear()
    signature = hidden_signature(
        client.post(
            "/api/v1/orders",
            json=order.body,
            headers=order.headers | {"X-Request-Token": "b" * 64},
        )
    )
    for token in (order.headers["X-Request-Token"], "b" * 64, "c" * 64):
        for body in (
            order.body,
            order.body | {"comment": "Synthetic changed description"},
        ):
            same(
                hidden_signature(
                    client.post(
                        "/api/v1/orders",
                        json=body,
                        headers=order.headers | {"X-Request-Token": token},
                    )
                ),
                signature,
            )
            same(graph(db).rows, erased.rows, "post-erasure replay")
    for suffix in (str(order.id), str(order.receipt["number"])):
        same(client.get("/api/v1/orders/" + suffix).status_code, 404)


@pytest.mark.parametrize(
    "boundary",
    [
        "consents",
        "orders",
        "order_items",
        "order_status_history",
        "email_outbox",
        "commit",
    ],
)
def test_creation_fault_rolls_back_then_same_key_completes_lifecycle(
    commercial_client: TestClient,
    commercial_db: Database,
    sink: Sink,
    boundary: str,
) -> None:
    client, db = commercial_client, commercial_db
    neighbor = accept(client, db, "support")
    before = graph(db)
    body, headers = commercial(db, ("DEMO-P-001", "DEMO-S-001")), order_headers()
    with pg_fault(
        db,
        "email_outbox" if boundary == "commit" else boundary,
        commit=boundary == "commit",
    ):
        response = client.post("/api/v1/orders", json=body, headers=headers)
    same(response.status_code, 503)
    same(graph(db).rows, before.rows, "atomic creation/quota rollback")
    target = accept(client, db, body=body, headers=headers)
    # The same rolled-back key can commit; its graph survives later operations.
    csrf = login(client)
    same(status(client, target, csrf, "in_progress", 1).status_code, 200)
    for _ in range(2):
        same(tick(db, configuration(sink), send_notification), True)
    replay(client, target)
    after = graph(db)
    same(
        after.business(neighbor.id, outbox=False),
        before.business(neighbor.id, outbox=False),
    )
    same(len(after.linked(target.id, "email_outbox")), 1)


def test_creation_lost_commit_ack_reconciles_original_receipt(
    commercial_client: TestClient,
    commercial_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db = commercial_client, commercial_db
    accept(client, db, "support")
    body, headers = commercial(db, ("DEMO-P-001", "DEMO-S-001")), order_headers()
    original, armed = db.unit_of_work, [False]

    def flushed(session: Session, context: object) -> None:
        if session.bind is db.engine and any(
            isinstance(row, Order) for row in session.new
        ):
            armed[0] = True

    @contextmanager
    def lost_ack() -> Iterator[Session]:
        with original() as session:
            yield session
        if armed[0]:
            armed[0] = False
            raise OperationalError(
                "synthetic lost commit acknowledgement",
                {},
                OSError("synthetic transport loss"),
            )

    event.listen(Session, "after_flush", flushed)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(db, "unit_of_work", lost_ack)
            result = client.post("/api/v1/orders", json=body, headers=headers)
    finally:
        event.remove(Session, "after_flush", flushed)
    same(result.status_code, 503)
    committed = graph(db)
    same(len(committed.rows["orders"]), 2, "actual committed state")
    catalog_change(db)
    replayed = client.post("/api/v1/orders", json=body, headers=headers)
    same(replayed.status_code, 200)
    same(replayed.json()["status"], "new")
    graph_after_catalog(committed, db)


def graph_after_catalog(before: Graph, database: Database) -> Graph:
    after = graph(database)
    for table in TABLE_BUSINESS:
        same(after.rows[table], before.rows[table], "lost-ack business graph")
    return after


TABLE_BUSINESS = (
    "orders",
    "order_items",
    "consents",
    "order_status_history",
    "email_outbox",
)


@pytest.mark.parametrize("failure", ["history", "audit", "commit"])
def test_status_fault_after_smtp_preserves_whole_graph(
    commercial_client: TestClient,
    commercial_db: Database,
    sink: Sink,
    failure: str,
) -> None:
    client, db = commercial_client, commercial_db
    target = accept(client, db)
    accept(client, db, "support")
    catalog_change(db)
    csrf = login(client)
    same(status(client, target, csrf, "in_progress", 1).status_code, 200)
    same(tick(db, configuration(sink), send_notification), True)
    before = graph(db)
    with pg_fault(
        db,
        "order_status_history" if failure == "history" else "audit_events",
        commit=failure == "commit",
    ):
        same(status(client, target, csrf, "closed", 2).status_code, 503)
    same(graph(db).rows, before.rows, "status rollback")
    same(status(client, target, csrf, "closed", 2).status_code, 200)
    after = graph(db)
    same(len(after.audits("order.status.change", str(target.id))), 2)
    replay(client, target)


@pytest.mark.parametrize("failure", ["audit", "commit"])
def test_manual_retry_fault_after_catalog_and_status(
    commercial_client: TestClient,
    commercial_db: Database,
    sink: Sink,
    failure: str,
) -> None:
    client, db = commercial_client, commercial_db
    order = accept(client, db)
    catalog_change(db)
    csrf = login(client)
    same(status(client, order, csrf, "in_progress", 1).status_code, 200)
    sink.mode = "reject"
    for attempt in range(5):
        if attempt:
            due(db, order.id)
        same(tick(db, configuration(sink), send_notification), True)
    before = graph(db)
    with pg_fault(db, "audit_events", commit=failure == "commit"):
        same(retry(client, order, csrf).status_code, 503)
    same(graph(db).rows, before.rows, "manual retry rollback")
    same(retry(client, order, csrf).status_code, 303)
    sink.mode = "accept"
    same(tick(db, configuration(sink), send_notification), True)
    same(
        graph(db).business(order.id, outbox=False),
        before.business(order.id, outbox=False),
    )
    replay(client, order)


@pytest.mark.parametrize("fault", ["messages", "session", "commit"])
def test_support_chat_cleanup_fault_manager_read_and_later_erasure(
    commercial_client: TestClient,
    commercial_db: Database,
    tmp_path: Path,
    fault: str,
) -> None:
    client, db = commercial_client, commercial_db
    own = client.post("/api/v1/chat/sessions", json={"mode": "knowledge"}).json()
    chat_id = UUID(own["session_id"])
    transcript = "Synthetic conversation kept out of confirmed support description"
    message = client.post(
        "/api/v1/chat/messages",
        json={
            "session_id": str(chat_id),
            "request_id": str(uuid4()),
            "expected_version": 0,
            "input": {"type": "text", "value": transcript},
        },
        headers={"X-Chat-Token": own["session_token"]},
    )
    same(message.status_code, 200)
    body = support_body() | {
        "source": "chat",
        "chat_session_id": str(chat_id),
        "comment": "  Synthetic confirmed\n<script>literal</script>  ",
    }
    order = accept(
        client,
        db,
        "support",
        body=body,
        headers=order_headers() | {"X-Chat-Token": own["session_token"]},
    )
    neighbor = accept(client, db)
    csrf = login(client)
    with db.unit_of_work() as session:
        chat = session.get(ChatSession, chat_id)
        assert chat is not None
        now = chat.created_at + timedelta(
            days=demo_bundle().retention.chat_storage_days
        )
    command = prepare(db, "chat_delete", chat_id, now)
    sink_journal = journal(tmp_path / "journal")
    proof = sink_journal.witness(command, synthetic=True)
    before = graph(db)
    with pg_fault(
        db,
        "chat_messages" if fault == "messages" else "chat_sessions",
        verb="DELETE FROM",
        commit=fault == "commit",
    ):
        with pytest.raises(DBAPIError):
            with db.unit_of_work() as session:
                same(apply_command(session, command, proof, now=now), True)
    same(graph(db).rows, before.rows, "chat cleanup rollback")
    result = reconcile_command(db, command, sink_journal, execute=True, now=now)
    same(result.commit_confirmed, True)
    after = graph(db)
    same(after.rows["chat_sessions"], [])
    same(after.rows["chat_messages"], [])
    same(after.order(order.id)["chat_session_id"], None)
    same(after.order(order.id)["comment"], body["comment"])
    same(transcript in str(after.order(order.id)), False)
    for table in ("consents", "order_items", "order_status_history", "email_outbox"):
        same(after.linked(order.id, table), before.linked(order.id, table))
    same(after.business(neighbor.id), before.business(neighbor.id))
    detail = client.get(f"/api/v1/admin/orders/{order.id}")
    same(detail.status_code, 200)
    same(detail.json()["comment"], body["comment"], "manager comment")
    same(
        reconcile_command(
            db, command, sink_journal, execute=True, now=now
        ).commit_confirmed,
        True,
    )
    same(graph(db).rows, after.rows, "idempotent chat cleanup")
    replay(client, order)
    erase_at = terminal(client, db, order, csrf)
    pii = prepare(db, "order_pii", order.id, erase_at)
    same(
        reconcile_command(
            db, pii, sink_journal, execute=True, now=erase_at
        ).commit_confirmed,
        True,
    )
    same(client.get(f"/api/v1/admin/orders/{order.id}").json()["comment"], None)
    hidden_signature(
        client.post("/api/v1/orders", json=order.body, headers=order.headers)
    )
    graph(db)


@contextmanager
def held_statement(
    database: Database, prefix: str
) -> Iterator[tuple[Event, Event, list[int]]]:
    """Pause one real backend after the specified SQL, while locks are held."""
    locked, release, pids = Event(), Event(), []

    def hold(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        many: bool,
    ) -> None:
        if statement.lstrip().startswith(prefix) and not locked.is_set():
            pids.append(
                connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            )
            locked.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic contention controller timed out")

    event.listen(database.engine, "after_cursor_execute", hold)
    try:
        yield locked, release, pids
    finally:
        release.set()
        event.remove(database.engine, "after_cursor_execute", hold)


@pytest.mark.parametrize("iteration", range(3))
@pytest.mark.parametrize("other_token", [False, True])
def test_parallel_post_unique_wait_then_status_and_worker(
    commercial_client: TestClient,
    commercial_db: Database,
    sink: Sink,
    iteration: int,
    other_token: bool,
) -> None:
    client, db = commercial_client, commercial_db
    neighbor = accept(client, db, "support")
    neighbor_before = graph(db).business(neighbor.id, outbox=False)
    body, headers = commercial(db, ("DEMO-P-001", "DEMO-S-001")), order_headers()
    altered = headers | {"X-Request-Token": "b" * 64} if other_token else headers
    with held_statement(db, "INSERT INTO app.orders") as (locked, release, pids):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                client.post, "/api/v1/orders", json=body, headers=headers
            )
            try:
                same(locked.wait(3), True, "first creation paused")
                second = pool.submit(
                    client.post, "/api/v1/orders", json=body, headers=altered
                )
                wait_for_blocker(db, pids[0])
            finally:
                release.set()
            a, b = first.result(5), second.result(5)
    same(a.status_code, 201)
    same(b.status_code, 404 if other_token else 200)
    if not other_token:
        same(a.json(), b.json(), "parallel receipt")
    else:
        hidden_signature(b)
    state = graph(db)
    same(tuple(len(state.rows[t]) for t in TABLE_BUSINESS), (2, 2, 2, 2, 2))
    identifier = UUID(
        next(
            row["id"]
            for row in state.rows["orders"]
            if row["idempotency_key"] == headers["Idempotency-Key"]
        )
    )
    target = Accepted(identifier, body, headers, a.json())
    replay(client, target)
    reordered = deepcopy(body)
    selections = reordered["items"]
    assert isinstance(selections, list)
    reordered["items"] = list(reversed(selections))
    same(
        client.post("/api/v1/orders", json=reordered, headers=headers).status_code, 409
    )
    same(
        client.post(
            "/api/v1/orders",
            json=body | {"comment": "Synthetic changed"},
            headers=headers,
        ).status_code,
        409,
    )
    catalog_change(db)
    csrf = login(client)
    same(status(client, target, csrf, "in_progress", 1).status_code, 200)
    for _ in range(2):
        same(tick(db, configuration(sink), send_notification), True)
    replay(client, target)
    same(graph(db).business(neighbor.id, outbox=False), neighbor_before)


@pytest.mark.parametrize("iteration", range(3))
def test_distinct_parallel_keys_unique_numbers_and_quota(
    commercial_client: TestClient,
    commercial_db: Database,
    iteration: int,
) -> None:
    client, db = commercial_client, commercial_db
    body = commercial(db, ("DEMO-P-001", "DEMO-S-001"))
    with held_statement(db, "INSERT INTO app.orders") as (locked, release, pids):
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(
                client.post, "/api/v1/orders", json=body, headers=order_headers()
            )
            try:
                same(locked.wait(3), True)
                b = pool.submit(
                    client.post, "/api/v1/orders", json=body, headers=order_headers()
                )
                wait_for_blocker(db, pids[0])
            finally:
                release.set()
            results = [a.result(5), b.result(5)]
    same([result.status_code for result in results], [201, 201])
    same(len({result.json()["number"] for result in results}), 2)
    for _ in range(3):
        accept(client, db, "support")
    full = graph(db)
    same(
        client.post("/api/v1/orders", json=body, headers=order_headers()).status_code,
        429,
    )
    same(graph(db).rows, full.rows, "quota rejection")
    same(tuple(len(full.rows[t]) for t in TABLE_BUSINESS), (5, 4, 5, 5, 5))


@pytest.mark.parametrize("iteration", range(3))
@pytest.mark.parametrize(
    "winner", ["creation", "catalog", "publication", "creation_catalog"]
)
def test_creation_contends_with_catalog_or_consent(
    commercial_client: TestClient,
    commercial_db: Database,
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
    iteration: int,
    winner: str,
) -> None:
    client, db = commercial_client, commercial_db
    neighbor = accept(client, db, "support")
    baseline = graph(db).business(neighbor.id)
    body, headers = commercial(db, ("DEMO-P-001", "DEMO-S-001")), order_headers()
    path, bundle = packet(tmp_path)
    if winner.startswith("creation"):
        with held_statement(db, "INSERT INTO app.orders") as (locked, release, pids):
            with ThreadPoolExecutor(max_workers=2) as pool:
                creating = pool.submit(
                    client.post, "/api/v1/orders", json=body, headers=headers
                )
                try:
                    same(locked.wait(3), True)
                    changing = (
                        pool.submit(catalog_change, db)
                        if winner == "creation_catalog"
                        else pool.submit(publish, privacy_db[1], path, bundle)
                    )
                    wait_for_blocker(db, pids[0])
                finally:
                    release.set()
                response = creating.result(5)
                outcome = changing.result(5)
                if winner == "creation":
                    assert outcome is not None
                    same(outcome.ok, True)
        same(response.status_code, 201)
        replayed = client.post("/api/v1/orders", json=body, headers=headers)
        same(replayed.status_code, 200)
        same(replayed.json(), response.json())
        same(len(graph(db).rows["consents"]), 2)
    else:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with (db if winner == "catalog" else privacy_db[1]).unit_of_work() as owner:
                if winner == "catalog":
                    issued = login_staff(owner, "privacy-admin", PASSWORD)
                    assert issued is not None
                    row = owner.scalars(
                        select(CatalogItem).where(CatalogItem.sku == "DEMO-P-001")
                    ).one()
                    CatalogWriteService(owner, issued.identity).change_item_state(
                        row.id, ItemState(expected_revision=row.revision), "archive"
                    )
                else:
                    same(
                        run_privacy(
                            owner,
                            login="privacy-admin",
                            password=PASSWORD,
                            operation="publish",
                            expected_version=DEMO_VERSION,
                            path=path,
                            expected_checksum=bundle.checksum(),
                        ).ok,
                        True,
                    )
                pid = owner.scalar(text("SELECT pg_backend_pid()"))
                assert pid is not None
                creating = pool.submit(
                    client.post, "/api/v1/orders", json=body, headers=headers
                )
                wait_for_blocker(db, pid)
            response = creating.result(5)
        same(response.status_code, 409)
        same(
            response.json()["error"]["code"],
            "CATALOG_CHANGED" if winner == "catalog" else "CONSENT_CHANGED",
        )
        same(len(graph(db).rows["orders"]), 1)
    same(graph(db).business(neighbor.id), baseline, "publication/catalog neighbor")


@pytest.mark.parametrize("iteration", range(3))
@pytest.mark.parametrize("erasure_first", [False, True])
def test_replay_contends_with_witnessed_erasure(
    commercial_client: TestClient,
    commercial_db: Database,
    tmp_path: Path,
    iteration: int,
    erasure_first: bool,
) -> None:
    client, db = commercial_client, commercial_db
    target = accept(client, db)
    neighbor = accept(client, db, "support")
    csrf = login(client)
    now = terminal(client, db, target, csrf)
    catalog_change(db)
    command = prepare(db, "order_pii", target.id, now)
    proof = journal(tmp_path / "journal").witness(command, synthetic=True)
    baseline = graph(db)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.unit_of_work() as owner:
            owner.scalar(select(Order).where(Order.id == target.id).with_for_update())
            if erasure_first:
                same(apply_command(owner, command, proof, now=now), True)
                future = pool.submit(
                    client.post,
                    "/api/v1/orders",
                    json=target.body,
                    headers=target.headers,
                )
            else:
                from app.modules.orders.service import replay_order
                from app.schemas.common import OrderHeaders
                from app.schemas.orders import OrderCreate

                receipt = replay_order(
                    owner,
                    OrderCreate.model_validate(target.body),
                    OrderHeaders.model_validate(target.headers),
                    app.state.order_security,
                )
                assert receipt is not None
                same(receipt.model_dump(mode="json"), target.receipt)

                def erase() -> bool:
                    with db.unit_of_work() as session:
                        return apply_command(session, command, proof, now=now)

                erasing = pool.submit(erase)
                same(erasing.result(3), False, "locked target deferred")
            if erasure_first:
                pid = owner.scalar(text("SELECT pg_backend_pid()"))
                assert pid is not None
                wait_for_blocker(db, pid)
        if erasure_first:
            hidden_signature(future.result(5))
        else:
            with db.unit_of_work() as session:
                same(apply_command(session, command, proof, now=now), True)
    erased = graph(db)
    same(erased.business(neighbor.id), baseline.business(neighbor.id))
    hidden_signature(
        client.post("/api/v1/orders", json=target.body, headers=target.headers)
    )
    same(graph(db).rows, erased.rows)


@pytest.mark.parametrize("iteration", range(3))
def test_two_staff_versions_on_created_graph_and_disabled_session(
    commercial_client: TestClient,
    commercial_db: Database,
    iteration: int,
) -> None:
    client, db = commercial_client, commercial_db
    target, neighbor = accept(client, db), accept(client, db, "support")
    catalog_change(db)
    identities = []
    for role in ("manager", "admin"):
        csrf = login(client, role)
        identities.append((csrf, client.cookies.get(SESSION_COOKIE)))
    client.cookies.clear()
    baseline = graph(db)

    def submit(identity: tuple[str, str | None]) -> Any:
        csrf, token = identity
        return client.patch(
            f"/api/v1/admin/orders/{target.id}/status",
            json={"status": "in_progress", "expected_version": 1},
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf,
                "Cookie": f"{SESSION_COOKIE}={token}",
            },
        )

    with held_statement(db, "UPDATE app.orders") as (locked, release, pids):
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(submit, identities[0])
            try:
                same(locked.wait(3), True)
                b = pool.submit(submit, identities[1])
                wait_for_blocker(db, pids[0])
            finally:
                release.set()
            same([a.result(5).status_code, b.result(5).status_code], [200, 409])
    current = graph(db)
    same(current.order(target.id)["version"], 2)
    audits = current.audits("order.status.change", str(target.id))
    same(len(audits), 1)
    same(
        audits[0]["metadata"],
        {
            "from_status": "new",
            "to_status": "in_progress",
            "from_version": 1,
            "to_version": 2,
        },
    )
    same(current.business(neighbor.id), baseline.business(neighbor.id))
    csrf = login(client)
    for headers in (
        {"Origin": ORIGIN},
        {"X-CSRF-Token": csrf},
        {"Origin": "https://foreign.invalid", "X-CSRF-Token": csrf},
    ):
        same(
            client.patch(
                f"/api/v1/admin/orders/{target.id}/status",
                json={
                    "status": "cancelled",
                    "expected_version": 2,
                    "reason": "Synthetic reason",
                },
                headers=headers,
            ).status_code,
            403,
        )
    same(status(client, target, csrf, "closed", 2).status_code, 200)
    terminal_state = graph(db)
    same(status(client, target, csrf, "in_progress", 3).status_code, 409)
    same(graph(db).rows, terminal_state.rows)
    with db.unit_of_work() as session:
        session.execute(
            update(StaffUser)
            .where(StaffUser.login == "privacy-manager")
            .values(is_active=False)
        )
    same(status(client, target, csrf, "cancelled", 3).status_code, 401)
    same(retry(client, target, csrf).status_code, 401)
    replay(client, target)
    graph(db)


@pytest.mark.parametrize(
    "boundary",
    ["before_smtp", "after_smtp", "lease_commit", "result_commit", "timeout"],
)
def test_worker_fault_after_status_and_catalog_preserves_graph(
    commercial_client: TestClient,
    commercial_db: Database,
    sink: Sink,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    client, db = commercial_client, commercial_db
    target = accept(client, db)
    catalog_change(db)
    csrf = login(client)
    same(status(client, target, csrf, "in_progress", 1).status_code, 200)
    before = graph(db)
    original_outbox = before.linked(target.id, "email_outbox")[0]
    if boundary in {"before_smtp", "after_smtp"}:
        script = (
            "import os; from app.core.db import Database,DatabaseSettings; "
            "from app.modules.notify.outbox import claim_due; "
            "from app.modules.notify.smtp import SMTPSettings,send_notification; "
            "db=Database(DatabaseSettings.from_environment())\n"
            "with db.unit_of_work() as session: claim=claim_due(session)\n"
            "assert claim is not None\n"
            f"settings=SMTPSettings('127.0.0.1',{sink.server_address[1]},'test',"
            "'notify@example.invalid','manager@example.invalid',"
            "'https://staff.example.invalid',frozenset({'https://staff.example.invalid'}),synthetic=True)\n"
            + (
                "send_notification(settings,claim)\n"
                if boundary == "after_smtp"
                else ""
            )
            + "os._exit(77)\n"
        )
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, timeout=10
        )
        same(process.returncode, 77, "real worker crash")
        same(graph(db).linked(target.id, "email_outbox")[0]["status"], "sending")
    elif boundary == "timeout":
        sink.mode = "timeout"
        with monkeypatch.context() as patch:
            patch.setattr("app.modules.notify.smtp.TIMEOUT", 0.1)
            same(tick(db, configuration(sink), send_notification), True)
        same(
            graph(db).linked(target.id, "email_outbox")[0]["last_error_code"],
            "SMTP_TIMEOUT",
        )
        sink.release.set()
        sink.mode = "accept"
    else:
        # The first UPDATE is the committed lease; the second is the result CAS.
        writes = 0

        def fail(
            connection: Connection,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            many: bool,
        ) -> None:
            nonlocal writes
            if statement.startswith("UPDATE app.email_outbox"):
                writes += 1
                if writes == (1 if boundary == "lease_commit" else 2):
                    connection.info["mvp044_commit_fault"] = True

        def committing(connection: Connection) -> None:
            if connection.info.pop("mvp044_commit_fault", False):
                connection.exec_driver_sql("SELECT 1/0")

        event.listen(db.engine, "after_cursor_execute", fail)
        event.listen(db.engine, "commit", committing)
        try:
            with pytest.raises(DBAPIError):
                tick(db, configuration(sink), send_notification)
        finally:
            event.remove(db.engine, "after_cursor_execute", fail)
            event.remove(db.engine, "commit", committing)
        same(
            graph(db).linked(target.id, "email_outbox")[0]["status"],
            "pending" if boundary == "lease_commit" else "sending",
        )
    same(
        graph(db).business(target.id, outbox=False),
        before.business(target.id, outbox=False),
    )
    if graph(db).linked(target.id, "email_outbox")[0]["status"] == "sending":
        due(db, target.id, expire=True)
        with db.unit_of_work() as session:
            same(recover_expired(session), 1)
    due(db, target.id)
    same(tick(db, configuration(sink), send_notification), True)
    after = graph(db)
    same(
        after.business(target.id, outbox=False),
        before.business(target.id, outbox=False),
    )
    same(after.linked(target.id, "email_outbox")[0]["status"], "sent")
    same(after.linked(target.id, "email_outbox")[0]["id"], original_outbox["id"])
    same(len(sink.messages), 2 if boundary in {"after_smtp", "result_commit"} else 1)
    same(
        {str(Parser().parsestr(message)["Message-ID"]) for message in sink.messages},
        {f"<outbox.{original_outbox['id']}@example.invalid>"},
    )
    replay(client, target)


@pytest.mark.parametrize("iteration", range(3))
def test_two_workers_lease_cas_and_protected_retry_states(
    commercial_client: TestClient,
    commercial_db: Database,
    iteration: int,
) -> None:
    client, db = commercial_client, commercial_db
    target = accept(client, db)
    neighbor = accept(client, db, "support")
    with db.unit_of_work() as session:
        session.execute(
            update(EmailOutbox)
            .where(EmailOutbox.order_id == neighbor.id)
            .values(next_attempt_at=database_now(session) + timedelta(days=365))
        )
    csrf = login(client)
    same(status(client, target, csrf, "in_progress", 1).status_code, 200)
    same(retry(client, target, csrf).status_code, 409)  # pending
    before, barrier, locked, release = graph(db), Barrier(2), Event(), Event()
    pids: list[int] = []

    def worker(first: bool) -> Claim | None:
        with db.unit_of_work() as session:
            pids.append(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
            barrier.wait(3)
            if not first:
                same(locked.wait(3), True)
            result = claim_due(session)
            if first:
                locked.set()
                same(release.wait(4), True)
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(worker, True), pool.submit(worker, False)
        try:
            same(locked.wait(3), True)
            same(b.result(3), None)
        finally:
            release.set()
        first = a.result(5)
    assert first is not None
    same(len(set(pids)), 2)
    same(retry(client, target, csrf).status_code, 409)  # sending
    due(db, target.id, expire=True)
    same(
        retry(client, target, csrf).status_code, 409
    )  # expired sending belongs to worker
    with db.unit_of_work() as session:
        same(recover_expired(session), 1)
    due(db, target.id)
    with db.unit_of_work() as session:
        second = claim_due(session)
    assert second is not None
    same(second.id, first.id)
    same(second.token == first.token, False)
    claimed = graph(db)
    with db.unit_of_work() as session:
        same(finish_success(session, first), False)
        same(finish_failure(session, first, "SMTP_TIMEOUT"), False)
    same(graph(db).rows, claimed.rows, "stale lease CAS")
    with db.unit_of_work() as session:
        same(finish_success(session, second), True)
    same(retry(client, target, csrf).status_code, 409)  # sent
    after = graph(db)
    same(
        after.business(target.id, outbox=False),
        before.business(target.id, outbox=False),
    )
    same(after.business(neighbor.id), before.business(neighbor.id))
    replay(client, target)


@pytest.mark.parametrize("failure", ["export", "apply", "commit", "lost_ack"])
def test_journal_faults_after_http_status_and_smtp(
    commercial_client: TestClient,
    commercial_db: Database,
    sink: Sink,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    client, db = commercial_client, commercial_db
    target, neighbor = accept(client, db), accept(client, db, "support")
    catalog_change(db)
    csrf = login(client)
    now = terminal(client, db, target, csrf)
    for _ in range(2):
        same(tick(db, configuration(sink), send_notification), True)
    command = prepare(db, "order_pii", target.id, now)
    sink_journal = journal(tmp_path / "journal")
    same(reconcile_command(db, command, sink_journal).state, JournalState.PREPARED)
    with pytest.raises(JournalUnavailable):
        with db.unit_of_work() as session:
            apply_command(session, command, command.canonical(), now=now)
    before = graph(db)
    if failure == "export":
        # The sink must perform a real filesystem operation which fails, rather
        # than returning a fabricated success/ack. Its private directory is absent.
        broken = SyntheticJournal(tmp_path / "absent")
        result = reconcile_command(db, command, broken, execute=True, now=now)
        same(result.commit_confirmed, False)
        same(graph(db).rows, before.rows, "export failure")
    else:
        proof = sink_journal.witness(command, synthetic=True)
        same(reconcile_command(db, command, sink_journal).state, JournalState.WITNESSED)
        if failure in {"apply", "commit"}:
            with pg_fault(db, "orders", verb="UPDATE", commit=failure == "commit"):
                result = reconcile_command(
                    db, command, sink_journal, execute=True, now=now
                )
            same(result.state, JournalState.WITNESSED)
            same(result.commit_confirmed, False)
            same(graph(db).rows, before.rows, "journal apply rollback")
        else:
            original, changed, raised = db.unit_of_work, [False], [0]

            def applied_write(
                connection: Connection,
                cursor: Any,
                statement: str,
                parameters: Any,
                context: Any,
                many: bool,
            ) -> None:
                if statement.startswith("UPDATE app.deletion_journal"):
                    changed[0] = True

            @contextmanager
            def lost_ack() -> Iterator[Session]:
                with original() as session:
                    yield session
                if changed[0]:
                    changed[0] = False
                    raised[0] += 1
                    raise OSError("Synthetic lost commit acknowledgement")

            event.listen(db.engine, "after_cursor_execute", applied_write)
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(db, "unit_of_work", lost_ack)
                    result = reconcile_command(
                        db, command, sink_journal, execute=True, now=now
                    )
            finally:
                event.remove(db.engine, "after_cursor_execute", applied_write)
            same(raised[0], 1, "lost journal acknowledgement seam reached")
            same(result.state, JournalState.APPLIED)
            same(result.commit_confirmed, True)
        same(proof.canonical, command.canonical())
    result = reconcile_command(db, command, sink_journal, execute=True, now=now)
    same(result.state, JournalState.APPLIED)
    same(result.commit_confirmed, True)
    applied = graph(db)
    same(applied.business(neighbor.id), before.business(neighbor.id))
    same(
        applied.linked(target.id, "order_items"),
        before.linked(target.id, "order_items"),
    )
    same(
        reconcile_command(
            db, command, sink_journal, execute=True, now=now
        ).commit_confirmed,
        True,
    )
    same(graph(db).rows, applied.rows, "journal exact retry")
    hidden_signature(
        client.post("/api/v1/orders", json=target.body, headers=target.headers)
    )


@pytest.mark.parametrize("bad_policy", ["missing", "corrupt"])
def test_historical_policy_no_latest_fallback_and_full_graph_fk(
    commercial_client: TestClient,
    commercial_db: Database,
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
    bad_policy: str,
) -> None:
    from app.modules.orders.privacy_models import ConsentPublication
    from app.modules.orders.privacy_service import PrivacyUnavailable

    client, db = commercial_client, commercial_db
    target, neighbor = accept(client, db), accept(client, db, "support")
    csrf = login(client)
    now = terminal(client, db, target, csrf)
    before = graph(db)
    path, bundle = packet(tmp_path)
    same(publish(privacy_db[1], path, bundle).ok, True)
    same(graph(db).linked(target.id, "consents"), before.linked(target.id, "consents"))
    # Model a damaged restore using transactional fixture damage. The migrator
    # remains subject to the actual immutable trigger (not disabled here).
    with pytest.raises(DBAPIError):
        with privacy_db[1].unit_of_work() as session:
            publication = session.get(ConsentPublication, DEMO_VERSION)
            assert publication is not None
            if bad_policy == "missing":
                session.delete(publication)
            else:
                publication.bundle_checksum = "0" * 64
            session.flush()
    # Genuine unavailable/evidence mismatch is tested without bypassing triggers:
    # an absent exact policy version cannot resolve to the newly published one.
    from app.modules.orders.cleanup_service import policy_for

    with db.unit_of_work() as session, pytest.raises(PrivacyUnavailable):
        policy_for(session, "SYNTHETIC-MISSING-044", now)
    pii = prepare(db, "order_pii", target.id, now)
    same(pii.policy_version, DEMO_VERSION)
    consent_id = UUID(before.order(target.id)["consent_id"])
    with pytest.raises(IntegrityError):
        with privacy_db[1].unit_of_work() as session:
            session.execute(
                text("DELETE FROM app.consents WHERE id=:id"), {"id": consent_id}
            )
    # Synthetic damaged-restore fixture: a historical consent may have no packet
    # or an untrustworthy packet. Insert fixture evidence, without disabling any
    # existing trigger or modifying the real immutable publication.
    damaged_version = "SYNTHETIC-044-DAMAGED-" + uuid4().hex
    damaged_bundle = demo_bundle().model_copy(
        update={
            "consent": demo_bundle().consent.model_copy(
                update={"version": damaged_version}
            )
        }
    )
    clone_id, clone_consent = uuid4(), uuid4()
    with privacy_db[1].unit_of_work() as session:
        session.add(
            ConsentText(
                **damaged_bundle.consent.model_dump(),
                active_from=utc_now() - timedelta(days=1),
            )
        )
        session.flush()
        if bad_policy == "corrupt":
            session.add(
                ConsentPublication(
                    consent_version=damaged_version,
                    bundle_checksum="0" * 64,
                    bundle=damaged_bundle.model_dump(mode="json"),
                    published_at=utc_now() - timedelta(days=1),
                )
            )
            session.flush()
        # Restore a copy with damaged collection evidence. Existing acceptance
        # evidence remains immutable; no trigger or FK is disabled. This copies
        # persisted rows, rather than implementing a second creation service.
        session.add(Consent(id=clone_consent, text_version=damaged_version))
        session.flush()
        for model in (Order, OrderItem, OrderStatusHistory, EmailOutbox):
            table = cast(Table, model.__table__)
            replacements: dict[str, Any] = {"id": func.gen_random_uuid()}
            if model is Order:
                replacements |= {
                    "id": literal(clone_id),
                    "consent_id": literal(clone_consent),
                    "idempotency_key": literal(uuid4()),
                }
                condition = table.c.id == target.id
            else:
                replacements["order_id"] = literal(clone_id)
                condition = table.c.order_id == target.id
            columns = [column for column in table.columns if column.name != "number"]
            session.execute(
                insert(table).from_select(
                    [column.name for column in columns],
                    select(
                        *[replacements.get(column.name, column) for column in columns]
                    ).where(condition),
                )
            )
    damaged = graph(db)
    with db.unit_of_work() as session, pytest.raises(PrivacyUnavailable):
        row = session.get(Order, clone_id)
        assert row is not None
        eligibility(session, "order_pii", row, now)
    same(graph(db).rows, damaged.rows, "dependent cleanup denied")
    # Immutable snapshots and partial PII erasure still fail real PG triggers/CHECK.
    for sql in (
        "UPDATE app.order_items SET qty=qty+1 WHERE order_id=:id",
        "UPDATE app.orders SET pii_erased_at=now() WHERE id=:id",
    ):
        with pytest.raises(DBAPIError):
            with db.unit_of_work() as session:
                session.execute(text(sql), {"id": target.id})
    migrator = privacy_db[1]
    with migrator.unit_of_work() as session:
        code, deletion = prepare_deletion(
            session,
            login="privacy-admin",
            password=PASSWORD,
            order_id=target.id,
            expected_version=3,
            decision_id=uuid4(),
            journal_policy_version=bundle.consent.version,
        )
    same(code, "PREPARED")
    assert deletion is not None
    sink_journal = journal(tmp_path / "journal")
    result = reconcile_command(
        migrator,
        deletion,
        sink_journal,
        execute=True,
        authorize_manual=lambda session: (
            authorize(session, "privacy-admin", PASSWORD) == "SUCCESS"
        ),
    )
    same(result.commit_confirmed, True)
    after = graph(db)
    same(after.business(neighbor.id), before.business(neighbor.id))
    same(any(row["id"] == str(target.id) for row in after.rows["orders"]), False)
    same(any(row["id"] == str(consent_id) for row in after.rows["consents"]), False)
    for table_name in ("order_items", "order_status_history", "email_outbox"):
        same(
            any(row["order_id"] == str(target.id) for row in after.rows[table_name]),
            False,
        )


@pytest.mark.parametrize("erased", [False, True])
def test_aq01_transport_and_hidden_matrix_after_cross_module_changes(
    commercial_client: TestClient,
    commercial_db: Database,
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
    erased: bool,
) -> None:
    client, db = commercial_client, commercial_db
    target = accept(client, db)
    neighbor = accept(client, db, "support")
    csrf = login(client)
    now = terminal(client, db, target, csrf)
    catalog_change(db)
    path, bundle = packet(tmp_path)
    same(publish(privacy_db[1], path, bundle).ok, True)
    # Missing outbox is an intentional privacy/retention state, never recreated
    # by replay; use the protected migrator, not expanded runtime privileges.
    with privacy_db[1].unit_of_work() as session:
        session.execute(delete(EmailOutbox).where(EmailOutbox.order_id == target.id))
    if erased:
        command = prepare(db, "order_pii", target.id, now)
        same(
            reconcile_command(
                db, command, journal(tmp_path / "journal"), execute=True, now=now
            ).commit_confirmed,
            True,
        )
    baseline = graph(db)
    hidden = hidden_signature(
        client.post(
            "/api/v1/orders",
            json=target.body,
            headers=target.headers | {"X-Request-Token": "f" * 64},
        )
    )
    for token in (target.headers["X-Request-Token"], "f" * 64, "e" * 64):
        for changed in (False, True):
            body = (
                target.body | {"comment": "Synthetic changed body"}
                if changed
                else target.body
            )
            response = client.post(
                "/api/v1/orders",
                json=body,
                headers=target.headers | {"X-Request-Token": token},
            )
            if erased or token != target.headers["X-Request-Token"]:
                same(hidden_signature(response), hidden)
            elif changed:
                same(response.status_code, 409)
                same(response.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")
            else:
                same(response.status_code, 200)
                same(response.json(), target.receipt)
            same(graph(db).rows, baseline.rows, "AQ-01 side effects")
    for name in ("Idempotency-Key", "X-Request-Token"):
        for value in (None, "invalid"):
            headers = dict(target.headers)
            if value is None:
                del headers[name]
            else:
                headers[name] = value
            same(
                client.post(
                    "/api/v1/orders", json=target.body, headers=headers
                ).status_code,
                422,
            )
            same(graph(db).rows, baseline.rows, "transport rejection")
    same(graph(db).business(neighbor.id), baseline.business(neighbor.id))
