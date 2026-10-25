"""Order creation, replay and locking on real PostgreSQL 16."""

import hashlib
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from threading import Barrier, Event, get_ident
from time import monotonic
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pytest import LogCaptureFixture, MonkeyPatch
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import ORMExecuteState, Session

from app.core.db import Database, DatabaseSettings
from app.core.db_types import utc_now
from app.main import app
from app.modules.catalog.models import CatalogItem, EquipmentModel
from app.modules.catalog.seed import seed_catalog
from app.modules.notify.models import EmailOutbox
from app.modules.orders.models import (
    Consent,
    ConsentText,
    Order,
    OrderItem,
    OrderStatusHistory,
)
from app.modules.orders.security import OrderSecurity
from app.modules.orders.seed import DEMO_VERSION, seed_consent
from app.schemas.orders import OrderCreate, OrderReceipt

pytestmark = pytest.mark.integration
WRITE_MODELS = (Consent, Order, OrderItem, OrderStatusHistory, EmailOutbox)


def support_body() -> dict[str, object]:
    return {
        "kind": "support",
        "items": [],
        "contact": {"name": "Synthetic Contact", "email": "synthetic@example.invalid"},
        "comment": "  Synthetic independent problem description  ",
        "source": "site",
        "consent": {"version": "SYNTHETIC-DEMO-ONLY-v1", "accepted": True},
        "confirmed": True,
    }


def order_headers() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4()), "X-Request-Token": "a" * 64}


@pytest.fixture
def creation_db(databases: tuple[Database, Database]) -> Iterator[Database]:
    runtime, migrator = databases
    with runtime.unit_of_work() as session:
        session.execute(update(ConsentText).values(retired_at=utc_now()))
    seed_consent(runtime, mode="test")
    with runtime.unit_of_work() as session:
        session.execute(
            update(ConsentText)
            .where(ConsentText.version == DEMO_VERSION)
            .values(retired_at=None)
        )
    seed_catalog(runtime, mode="test")
    try:
        yield runtime
    finally:
        with migrator.unit_of_work() as session:
            session.execute(delete(Order))
            session.execute(delete(Consent))
            session.execute(delete(CatalogItem))
            session.execute(delete(EquipmentModel))
            session.execute(update(ConsentText).values(retired_at=utc_now()))


@pytest.fixture
def creation_client(creation_db: Database) -> Iterator[TestClient]:
    with TestClient(app) as client:
        app.state.database = creation_db
        app.state.order_security = OrderSecurity(b"synthetic-integration-key" * 2)
        yield client


def commercial(database: Database, skus: tuple[str, ...]) -> dict[str, object]:
    with database.unit_of_work() as session:
        rows = list(
            session.scalars(
                select(CatalogItem)
                .where(CatalogItem.sku.in_(skus))
                .order_by(CatalogItem.sku)
            )
        )
        return support_body() | {
            "kind": "commercial",
            "items": [
                {"item_id": str(row.id), "qty": 2, "expected_revision": row.revision}
                for row in rows
            ],
        }


def counts(database: Database) -> tuple[int, ...]:
    with database.unit_of_work() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model)) or 0
            for model in WRITE_MODELS
        )


@pytest.mark.parametrize(
    "skus,known,items,delivery",
    [
        (("DEMO-P-001", "DEMO-S-001"), "382.68", "382.68", None),
        (("DEMO-P-001",), "246.90", "246.90", None),
        (("DEMO-S-001",), "135.78", "135.78", "0.00"),
        (("DEMO-P-003",), "0.00", "0.00", "0.00"),
        (("DEMO-P-002",), "0.00", None, None),
        (("DEMO-P-001", "DEMO-P-002", "DEMO-S-001"), "382.68", None, None),
        ((), "0.00", None, "0.00"),
    ],
)
def test_complete_http_creation(
    creation_client: TestClient,
    creation_db: Database,
    skus: tuple[str, ...],
    known: str,
    items: str | None,
    delivery: str | None,
    caplog: LogCaptureFixture,
) -> None:
    body = commercial(creation_db, skus) if skus else support_body()
    headers = order_headers()
    before = utc_now()
    response = creation_client.post("/api/v1/orders", json=body, headers=headers)
    assert response.status_code == 201, response.text
    result = OrderReceipt.model_validate_json(response.content)
    assert result.known_subtotal == Decimal(known)
    assert result.items_total == (Decimal(items) if items is not None else None)
    assert result.delivery_cost == (Decimal(delivery) if delivery is not None else None)
    assert result.delivery_status == (
        "pending_quote" if delivery is None else "not_required"
    )
    assert result.total_amount == (
        Decimal(items) if items is not None and delivery is not None else None
    )
    assert response.json()["known_subtotal"] == known
    assert response.headers["cache-control"] == "no-store"
    assert counts(creation_db) == (1, 1, len(skus), 1, 1)
    with creation_db.unit_of_work() as session:
        order = session.scalars(select(Order)).one()
        consent = session.get(Consent, order.consent_id)
        assert consent is not None and consent.text_version == DEMO_VERSION
        assert before <= consent.granted_at <= utc_now()
        assert consent.channel == "request_form" and consent.revoked_at is None
        assert (
            order.number == result.number
            and order.version == 1
            and order.source == "site"
        )
        assert order.status == "new" and order.comment == body["comment"]
        assert (
            order.receipt_token_hash
            == hashlib.sha256(headers["X-Request-Token"].encode()).hexdigest()
        )
        assert order.request_hash is not None and len(order.request_hash) == 64
        assert order.request_hash == app.state.order_security.request_hash(
            OrderCreate.model_validate(body)
        )
        history = session.scalars(select(OrderStatusHistory)).one()
        assert (
            history.from_status,
            history.to_status,
            history.changed_by,
            history.reason,
        ) == (None, "new", None, None)
        outbox = session.scalars(select(EmailOutbox)).one()
        assert (
            outbox.status == "pending"
            and outbox.event_kind == "manager_notification"
            and outbox.attempts == 0
        )
        assert outbox.lease_token is None and outbox.last_error_code is None
        outbox_json = json.dumps(
            dict(
                session.execute(
                    text("SELECT row_to_json(e) FROM app.email_outbox e")
                ).one()[0]
            ),
            default=str,
        )
        audit_json = str(
            list(session.execute(text("SELECT row_to_json(a) FROM app.audit_events a")))
        )
        for snapshot, catalog in session.execute(
            select(OrderItem, CatalogItem).join(
                CatalogItem, CatalogItem.id == OrderItem.item_id
            )
        ):
            assert snapshot.qty == 2
            for snap, field in (
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
                assert getattr(snapshot, snap + "_snapshot") == getattr(catalog, field)
    for private in (
        "Synthetic Contact",
        "synthetic@example.invalid",
        str(body["comment"]),
        headers["X-Request-Token"],
    ):
        assert private not in response.text + outbox_json + audit_json + caplog.text


@pytest.mark.parametrize(
    "state,code,status",
    [
        ("changed", "CONSENT_CHANGED", 409),
        ("retired", "SERVICE_UNAVAILABLE", 503),
        ("future", "CONSENT_CHANGED", 409),
        ("ambiguous", "SERVICE_UNAVAILABLE", 503),
        ("checksum", "SERVICE_UNAVAILABLE", 503),
        ("missing", "CONSENT_CHANGED", 409),
        ("outdated", "CONSENT_CHANGED", 409),
    ],
)
def test_consent_fail_closed(
    creation_client: TestClient,
    creation_db: Database,
    state: str,
    code: str,
    status: int,
) -> None:
    body = support_body()
    submitted = "SYNTHETIC-SUBMITTED-" + uuid4().hex
    if state in {"changed", "missing", "future", "outdated"}:
        body["consent"] = {"version": submitted, "accepted": True}
    with creation_db.unit_of_work() as session:
        if state in {"retired", "checksum"}:
            session.execute(update(ConsentText).values(retired_at=utc_now()))
        if state in {"future", "ambiguous", "checksum", "outdated"}:
            content = "Synthetic test only, not legally approved"
            session.add(
                ConsentText(
                    version=submitted,
                    purpose="SYNTHETIC TEST ONLY",
                    text=content,
                    checksum="0" * 64
                    if state == "checksum"
                    else hashlib.sha256(content.encode()).hexdigest(),
                    active_from=utc_now() + timedelta(days=1)
                    if state == "future"
                    else utc_now() - timedelta(days=1),
                    retired_at=utc_now() if state == "outdated" else None,
                )
            )
    response = creation_client.post(
        "/api/v1/orders", json=body, headers=order_headers()
    )
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert counts(creation_db) == (0, 0, 0, 0, 0)
    get = creation_client.get("/api/v1/consent-text")
    assert get.status_code == (
        503 if state in {"retired", "ambiguous", "checksum"} else 200
    )
    assert get.headers["cache-control"] == "no-store"


def test_get_is_read_only_and_synthetic_guard(
    creation_client: TestClient, creation_db: Database, monkeypatch: MonkeyPatch
) -> None:
    with creation_db.unit_of_work() as session:
        before = session.scalar(select(func.count()).select_from(ConsentText))
    for _ in range(2):
        response = creation_client.get("/api/v1/consent-text")
        assert response.status_code == 200
        assert set(response.json()) == {"version", "purpose", "text", "checksum"}
        assert "NOT LEGALLY APPROVED" in response.json()["purpose"]
    monkeypatch.setenv("APP_ENV", "production")
    assert creation_client.get("/api/v1/consent-text").status_code == 503
    assert (
        creation_client.post(
            "/api/v1/orders", json=support_body(), headers=order_headers()
        ).status_code
        == 503
    )
    with creation_db.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(ConsentText)) == before


@pytest.mark.parametrize("state", ["draft", "archived", "deleted", "revised"])
def test_catalog_conflicts(
    creation_client: TestClient, creation_db: Database, state: str
) -> None:
    body = commercial(creation_db, ("DEMO-P-001", "DEMO-S-001"))
    with creation_db.unit_of_work() as session:
        query = CatalogItem.sku == "DEMO-P-001"
        if state == "deleted":
            session.execute(delete(CatalogItem).where(query))
        else:
            changes = (
                {"revision": 2, "price": Decimal("999.99")}
                if state == "revised"
                else {"status": state}
            )
            session.execute(update(CatalogItem).where(query).values(**changes))
    response = creation_client.post(
        "/api/v1/orders", json=body, headers=order_headers()
    )
    assert (
        response.status_code == 409
        and response.json()["error"]["code"] == "CATALOG_CHANGED"
    )
    assert counts(creation_db) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize("stage", WRITE_MODELS)
def test_failure_at_each_write_rolls_back(
    creation_client: TestClient, creation_db: Database, stage: type[object]
) -> None:
    body = commercial(creation_db, ("DEMO-P-001", "DEMO-S-001"))

    def fail(session: Session, context: object, instances: object) -> None:
        if any(isinstance(row, stage) for row in session.new):
            raise OperationalError(
                "safe-injected", {}, Exception("private database detail")
            )

    event.listen(Session, "before_flush", fail)
    try:
        response = creation_client.post(
            "/api/v1/orders", json=body, headers=order_headers()
        )
    finally:
        event.remove(Session, "before_flush", fail)
    assert response.status_code == 503
    assert "private database detail" not in response.text
    assert counts(creation_db) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize("failure", ["unique", "fk", "commit"])
def test_late_constraint_or_commit_failure(
    creation_client: TestClient, creation_db: Database, failure: str
) -> None:
    def fail(session: Session, context: object, instances: object) -> None:
        for row in list(session.new):
            if isinstance(row, EmailOutbox):
                if failure == "unique":
                    session.add(
                        EmailOutbox(
                            order_id=row.order_id, event_kind="manager_notification"
                        )
                    )
                if failure == "fk":
                    row.order_id = uuid4()

    def fail_commit(connection: Connection) -> None:
        raise OperationalError("safe-injected", {}, Exception("private commit detail"))

    event.listen(
        creation_db.engine, "commit", fail_commit
    ) if failure == "commit" else event.listen(Session, "before_flush", fail)
    try:
        response = creation_client.post(
            "/api/v1/orders", json=support_body(), headers=order_headers()
        )
    finally:
        event.remove(
            creation_db.engine, "commit", fail_commit
        ) if failure == "commit" else event.remove(Session, "before_flush", fail)
    assert response.status_code == 503
    assert counts(creation_db) == (0, 0, 0, 0, 0)


def test_overflow(creation_client: TestClient, creation_db: Database) -> None:
    with creation_db.unit_of_work() as session:
        session.execute(
            update(CatalogItem)
            .where(CatalogItem.sku == "DEMO-S-001")
            .values(price=Decimal("9999999999.99"))
        )
    response = creation_client.post(
        "/api/v1/orders",
        json=commercial(creation_db, ("DEMO-S-001",)),
        headers=order_headers(),
    )
    assert response.status_code == 422
    assert counts(creation_db) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize("duplicate", [False, True])
def test_concurrent_requests_and_canonical_array_order(
    creation_client: TestClient, creation_db: Database, duplicate: bool
) -> None:
    body = commercial(creation_db, ("DEMO-P-001", "DEMO-S-001"))
    selections = body["items"]
    assert isinstance(selections, list)
    reverse = body | {"items": list(reversed(selections))}
    headers = order_headers()

    def submit(index: int) -> int:
        response = creation_client.post(
            "/api/v1/orders",
            json=body if index % 2 else reverse,
            headers=headers if duplicate else order_headers(),
        )
        assert "synthetic@example.invalid" not in response.text
        return response.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(submit, range(4)))
    accepted = 1 if duplicate else 4
    assert sorted(statuses) == ([200, 201, 409, 409] if duplicate else [201] * 4)
    assert counts(creation_db) == (accepted, accepted, accepted * 2, accepted, accepted)
    if duplicate:
        with creation_db.unit_of_work() as session:
            saved = session.scalars(select(Order)).one().request_hash
        winning_body = next(
            candidate
            for candidate in (body, reverse)
            if app.state.order_security.request_hash(
                OrderCreate.model_validate(candidate)
            )
            == saved
        )
        for token in (headers["X-Request-Token"], "b" * 64):
            response = creation_client.post(
                "/api/v1/orders",
                json=winning_body,
                headers=headers | {"X-Request-Token": token},
            )
            assert response.status_code == (
                200 if token == headers["X-Request-Token"] else 404
            )


@pytest.mark.parametrize("writer_first", [False, True])
def test_catalog_update_serializes_with_snapshot(
    creation_client: TestClient, creation_db: Database, writer_first: bool
) -> None:
    body = commercial(creation_db, ("DEMO-S-001",))
    locked = Event()
    release = Event()

    def pause(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        many: bool,
    ) -> None:
        if "catalog_items" in statement and (
            (writer_first and statement.startswith("UPDATE"))
            or (not writer_first and "FOR UPDATE" in statement)
        ):
            locked.set()
            assert release.wait(10)

    event.listen(creation_db.engine, "after_cursor_execute", pause)

    def edit() -> None:
        with creation_db.unit_of_work() as session:
            session.execute(
                update(CatalogItem)
                .where(CatalogItem.sku == "DEMO-S-001")
                .values(price=Decimal("99.99"), revision=2)
            )

    def submit() -> int:
        return creation_client.post(
            "/api/v1/orders", json=body, headers=order_headers()
        ).status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(edit if writer_first else submit)
            assert locked.wait(10)
            second = pool.submit(submit if writer_first else edit)
            deadline = monotonic() + 5
            waiting = False
            while monotonic() < deadline:
                with creation_db.engine.connect() as connection:
                    waiting = bool(
                        connection.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE usename=current_user AND wait_event_type='Lock' "
                                "AND cardinality(pg_blocking_pids(pid))>0)"
                            )
                        )
                    )
                if waiting:
                    break
                release.wait(0.02)
            release.set()
            first_result, second_result = first.result(15), second.result(15)
            assert waiting, "The competing SQL statement must actually wait on a lock"
    finally:
        release.set()
        event.remove(creation_db.engine, "after_cursor_execute", pause)
    status = second_result if writer_first else first_result
    assert status == (409 if writer_first else 201)
    with creation_db.unit_of_work() as session:
        snapshot = session.scalar(select(OrderItem))
        if writer_first:
            assert snapshot is None
        else:
            assert snapshot is not None
            assert (
                snapshot.unit_price_snapshot == Decimal("67.89")
                and snapshot.catalog_revision_snapshot == 1
            )
        current = session.scalars(
            select(CatalogItem).where(CatalogItem.sku == "DEMO-S-001")
        ).one()
        assert current.price == Decimal("99.99") and current.revision == 2


@pytest.mark.parametrize("mode", ["production", "demo", ""])
def test_synthetic_requires_matching_marker(
    creation_client: TestClient, monkeypatch: MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("APP_ENV", mode)
    assert creation_client.get("/api/v1/consent-text").status_code == 503


def test_missing_security_and_unreachable_database(creation_client: TestClient) -> None:
    app.state.order_security = None
    response = creation_client.post(
        "/api/v1/orders", json=support_body(), headers=order_headers()
    )
    assert response.status_code == 503
    app.state.order_security = OrderSecurity(b"synthetic-only-key" * 3)
    unavailable = Database(
        DatabaseSettings(
            host="127.0.0.1",
            port=1,
            database="synthetic",
            user="synthetic",
            password="synthetic-test-password",
        )
    )
    try:
        app.state.database = unavailable
        for path in ("/api/v1/consent-text", "/api/v1/orders"):
            response = (
                creation_client.get(path)
                if path.endswith("text")
                else creation_client.post(
                    path, json=support_body(), headers=order_headers()
                )
            )
            assert response.status_code == 503
            assert "synthetic-test-password" not in response.text
    finally:
        unavailable.close()


def test_support_phone_contact_preserves_description(
    creation_client: TestClient, creation_db: Database
) -> None:
    body = support_body() | {
        "contact": {"name": "Synthetic Contact", "phone": "+70000000000"}
    }
    response = creation_client.post(
        "/api/v1/orders", json=body, headers=order_headers()
    )
    assert response.status_code == 201
    with creation_db.unit_of_work() as session:
        order = session.scalars(select(Order)).one()
        assert order.phone == "+70000000000" and order.email is None
        assert order.comment == body["comment"]


def test_service_unknown_without_shipping(
    creation_client: TestClient, creation_db: Database
) -> None:
    with creation_db.unit_of_work() as session:
        session.execute(
            update(CatalogItem)
            .where(CatalogItem.sku == "DEMO-S-001")
            .values(price_mode="on_request", price=None)
        )
    response = creation_client.post(
        "/api/v1/orders",
        json=commercial(creation_db, ("DEMO-S-001",)),
        headers=order_headers(),
    )
    assert response.status_code == 201
    assert response.json()["known_subtotal"] == "0.00"
    assert response.json()["items_total"] is None
    assert response.json()["delivery_cost"] == "0.00"
    assert response.json()["total_amount"] is None


def erase_fixture(database: Database) -> None:
    """Synthetic atomic erasure only; the production worker remains MVP-038."""
    with database.unit_of_work() as session:
        session.execute(
            update(Order).values(
                pii_erased_at=utc_now(),
                organization_name=None,
                contact_name=None,
                email=None,
                phone=None,
                city=None,
                comment=None,
                request_hash=None,
                receipt_token_hash=None,
            )
        )
        session.execute(update(OrderStatusHistory).values(reason=None))


@pytest.mark.parametrize("skus", [(), ("DEMO-S-001",), ("DEMO-P-001", "DEMO-P-002")])
def test_replay_original_receipt_after_all_mutable_state_changes(
    creation_client: TestClient,
    creation_db: Database,
    skus: tuple[str, ...],
    caplog: LogCaptureFixture,
) -> None:
    body = commercial(creation_db, skus) if skus else support_body()
    headers = order_headers()
    first = creation_client.post("/api/v1/orders", json=body, headers=headers)
    assert first.status_code == 201
    assert counts(creation_db) == (1, 1, len(skus), 1, 1)
    again = creation_client.post("/api/v1/orders", json=body, headers=headers)
    assert again.status_code == 200 and again.json() == first.json()
    assert counts(creation_db) == (1, 1, len(skus), 1, 1)
    with creation_db.unit_of_work() as session:
        session.execute(
            update(CatalogItem)
            .where(CatalogItem.price_mode == "fixed")
            .values(
                price=Decimal("999.00"),
                revision=2,
                status="archived",
            )
        )
        session.execute(update(ConsentText).values(retired_at=utc_now()))
        content = "Synthetic replacement only"
        session.add(
            ConsentText(
                version="synthetic-replacement-" + uuid4().hex,
                purpose="SYNTHETIC",
                text=content,
                checksum=hashlib.sha256(content.encode()).hexdigest(),
                active_from=utc_now() - timedelta(seconds=1),
                retired_at=None,
            )
        )
        session.execute(update(Order).values(status="in_progress", version=2))
        session.execute(delete(EmailOutbox))
    again = creation_client.post("/api/v1/orders", json=body, headers=headers)
    assert again.status_code == 200 and again.json() == first.json()
    assert counts(creation_db) == (1, 1, len(skus), 1, 0)
    for changed in (
        body | {"city": None},
        body | {"comment": "Changed synthetic body"},
        body | {"consent": {"version": "replacement", "accepted": True}},
    ):
        conflict = creation_client.post("/api/v1/orders", json=changed, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    for private in (
        "Synthetic Contact",
        "synthetic@example.invalid",
        headers["X-Request-Token"],
    ):
        assert private not in first.text + again.text + caplog.text
    assert counts(creation_db) == (1, 1, len(skus), 1, 0)


@pytest.mark.parametrize("erased", [False, True])
@pytest.mark.parametrize(
    "catalog_changed,consent_changed,outbox_deleted",
    [(a, b, c) for a in (False, True) for b in (False, True) for c in (False, True)],
)
def test_hidden_object_matrix(
    creation_client: TestClient,
    creation_db: Database,
    erased: bool,
    catalog_changed: bool,
    consent_changed: bool,
    outbox_deleted: bool,
) -> None:
    body = commercial(creation_db, ("DEMO-S-001",))
    headers = order_headers()
    assert (
        creation_client.post("/api/v1/orders", json=body, headers=headers).status_code
        == 201
    )
    if erased:
        erase_fixture(creation_db)
    with creation_db.unit_of_work() as session:
        if catalog_changed:
            session.execute(
                update(CatalogItem)
                .where(CatalogItem.price_mode == "fixed")
                .values(
                    price=Decimal("999.99"),
                    revision=2,
                    status="archived",
                )
            )
        if consent_changed:
            session.execute(update(ConsentText).values(retired_at=utc_now()))
        if outbox_deleted:
            session.execute(delete(EmailOutbox))
        order = session.scalars(select(Order)).one()
        identifier = order.id
        if erased:
            assert order.request_hash is order.receipt_token_hash is None
            assert order.idempotency_key is not None
    hidden = creation_client.get("/api/v1/orders/" + str(identifier))
    assert hidden.status_code == 404
    tokens = ("a" * 64, "b" * 64, "c" * 64) if erased else ("b" * 64, "c" * 64)
    before = counts(creation_db)
    for token in tokens:
        for submitted in (body, body | {"comment": "Changed synthetic body"}):
            response = creation_client.post(
                "/api/v1/orders",
                json=submitted,
                headers=headers | {"X-Request-Token": token},
            )
            assert response.status_code == 404
            assert (
                response.json()["error"]
                == hidden.json()["error"]
                == {
                    "code": "NOT_FOUND",
                    "message": "Object not found",
                    "details": {},
                }
            )
            assert set(response.json()) == {"error", "request_id"}
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["request_id"] == response.headers["x-request-id"]
            assert response.headers.keys() == hidden.headers.keys()
    assert counts(creation_db) == before


@pytest.mark.parametrize("state", ["unused", "retained", "erased"])
def test_transport_matrix(
    creation_client: TestClient, creation_db: Database, state: str
) -> None:
    body, headers = support_body(), order_headers()
    if state != "unused":
        assert (
            creation_client.post(
                "/api/v1/orders", json=body, headers=headers
            ).status_code
            == 201
        )
    if state == "erased":
        erase_fixture(creation_db)
    before = counts(creation_db)
    for field, value in (
        ("Idempotency-Key", None),
        ("Idempotency-Key", "invalid"),
        ("X-Request-Token", None),
        ("X-Request-Token", "bad"),
        ("X-Request-Token", "A" * 64),
        ("X-Request-Token", "g" * 64),
    ):
        changed = headers.copy()
        if value is None:
            del changed[field]
        else:
            changed[field] = value
        for submitted in (body, body | {"comment": "Changed synthetic body"}):
            response = creation_client.post(
                "/api/v1/orders", json=submitted, headers=changed
            )
            assert response.status_code == 422
            assert response.json()["error"] == {
                "code": "VALIDATION_ERROR",
                "message": "Invalid request parameters",
                "details": {},
            }
    assert counts(creation_db) == before


@pytest.mark.parametrize("mode", ["identical", "changed", "different_tokens"])
def test_unique_race_rolls_back_before_new_session_reads_winner(
    creation_client: TestClient,
    creation_db: Database,
    mode: str,
) -> None:
    headers, body = order_headers(), commercial(creation_db, ("DEMO-S-001",))
    barrier = Barrier(4)
    traces: dict[int, list[tuple[str, int]]] = {}

    def lookup(state: ORMExecuteState) -> None:
        statement = str(state.statement)
        if "app.orders" in statement and "FOR UPDATE" in statement:
            trace = traces.setdefault(get_ident(), [])
            trace.append(("lookup", id(state.session)))

    def pause(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        many: bool,
    ) -> None:
        if "app.orders" in statement and "FOR UPDATE" in statement:
            if len(traces[get_ident()]) == 1:
                barrier.wait(10)  # All four SELECTs really saw the unused key.

    def rollback(session: Session) -> None:
        trace = traces.get(get_ident())
        if trace is not None:
            trace.append(("rollback", id(session)))

    event.listen(Session, "do_orm_execute", lookup)
    event.listen(Session, "after_rollback", rollback)
    event.listen(creation_db.engine, "after_cursor_execute", pause)

    def submit(index: int) -> tuple[int, dict[str, object], dict[str, str]]:
        submitted = deepcopy(body)
        auth = headers.copy()
        if mode == "changed":
            submitted["comment"] = f"Synthetic variant {index}"
        if mode == "different_tokens":
            auth["X-Request-Token"] = str(index + 1) * 64
        response = creation_client.post("/api/v1/orders", json=submitted, headers=auth)
        return response.status_code, submitted, auth

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(submit, range(4)))
    finally:
        event.remove(Session, "do_orm_execute", lookup)
        event.remove(Session, "after_rollback", rollback)
        event.remove(creation_db.engine, "after_cursor_execute", pause)
    loser_status = {"identical": 200, "changed": 409, "different_tokens": 404}[mode]
    assert sorted(row[0] for row in results) == sorted([201, *([loser_status] * 3)])
    loser_traces = [trace for trace in traces.values() if len(trace) > 1]
    assert len(loser_traces) == 3
    for trace in loser_traces:
        assert [entry[0] for entry in trace[:3]] == ["lookup", "rollback", "lookup"]
        assert trace[0][1] == trace[1][1] and trace[0][1] != trace[2][1]
    assert counts(creation_db) == (1, 1, 1, 1, 1)
    _, winning_body, winning_headers = next(row for row in results if row[0] == 201)
    assert (
        creation_client.post(
            "/api/v1/orders", json=winning_body, headers=winning_headers
        ).status_code
        == 200
    )


@pytest.mark.parametrize("eraser_first", [False, True])
def test_replay_serializes_with_real_erasure(
    creation_client: TestClient,
    creation_db: Database,
    eraser_first: bool,
) -> None:
    body, headers = support_body(), order_headers()
    first = creation_client.post("/api/v1/orders", json=body, headers=headers)
    assert first.status_code == 201
    locked, release = Event(), Event()

    def pause(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        many: bool,
    ) -> None:
        if "app.orders" in statement and (
            (eraser_first and statement.startswith("UPDATE"))
            or (not eraser_first and "FOR UPDATE" in statement)
        ):
            locked.set()
            assert release.wait(10)

    def replay() -> int:
        response = creation_client.post("/api/v1/orders", json=body, headers=headers)
        if response.status_code == 200:
            assert response.json() == first.json()
        return response.status_code

    event.listen(creation_db.engine, "after_cursor_execute", pause)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = (
                pool.submit(erase_fixture, creation_db)
                if eraser_first
                else pool.submit(replay)
            )
            assert locked.wait(10)
            b = (
                pool.submit(replay)
                if eraser_first
                else pool.submit(erase_fixture, creation_db)
            )
            deadline, waiting = monotonic() + 2, False
            while monotonic() < deadline:
                with creation_db.engine.connect() as connection:
                    waiting = bool(
                        connection.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE usename=current_user AND wait_event_type='Lock' "
                                "AND cardinality(pg_blocking_pids(pid))>0)"
                            )
                        )
                    )
                if waiting:
                    break
                release.wait(0.01)
            release.set()
            a_result, b_result = a.result(15), b.result(15)
            assert waiting
            assert (b_result if eraser_first else a_result) == (
                404 if eraser_first else 200
            )
    finally:
        release.set()
        event.remove(creation_db.engine, "after_cursor_execute", pause)
    assert replay() == 404
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: replay(), range(4))) == [404] * 4
    assert counts(creation_db) == (1, 1, 0, 1, 1)


def test_rotation_and_expiry_on_postgresql(
    creation_client: TestClient,
    creation_db: Database,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    path = tmp_path / "hmac-secret"
    old, active = "synthetic-old-key-" * 3, "synthetic-active-key-" * 3
    path.write_text(old)
    monkeypatch.setenv("ORDER_HMAC_SECRET_FILE", str(path))
    app.state.order_security = OrderSecurity.from_environment()
    body, headers = support_body(), order_headers()
    first = creation_client.post("/api/v1/orders", json=body, headers=headers)
    assert first.status_code == 201
    with creation_db.unit_of_work() as session:
        original_hash = session.scalars(select(Order)).one().request_hash
    path.write_text(json.dumps({"active": active, "previous": [old]}))
    app.state.order_security = OrderSecurity.from_environment()
    assert app.state.order_security is not None
    replay = creation_client.post("/api/v1/orders", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    with creation_db.unit_of_work() as session:
        assert session.scalars(select(Order)).one().request_hash == original_hash
        session.execute(
            update(Order).values(
                created_at=utc_now() - timedelta(hours=24) + timedelta(seconds=10)
            )
        )
    assert (
        creation_client.post("/api/v1/orders", json=body, headers=headers).status_code
        == 200
    )
    newer_headers = order_headers()
    assert (
        creation_client.post(
            "/api/v1/orders", json=body, headers=newer_headers
        ).status_code
        == 201
    )
    with creation_db.unit_of_work() as session:
        newer = session.scalars(
            select(Order).where(
                Order.idempotency_key == newer_headers["Idempotency-Key"]
            )
        ).one()
        assert newer.request_hash == OrderSecurity(active.encode()).request_hash(
            OrderCreate.model_validate(body)
        )
        session.execute(
            update(Order).values(created_at=utc_now() - timedelta(hours=24, seconds=1))
        )
    path.write_text(json.dumps({"active": active, "previous": []}))
    app.state.order_security = OrderSecurity.from_environment()
    for submitted in (body, body | {"city": "Synthetic city"}):
        assert (
            creation_client.post(
                "/api/v1/orders", json=submitted, headers=headers
            ).status_code
            == 404
        )
    assert counts(creation_db) == (2, 2, 0, 2, 2)


def test_timing_review_and_no_hmac_for_hidden_paths(
    creation_client: TestClient,
    creation_db: Database,
    monkeypatch: MonkeyPatch,
) -> None:
    body, headers = support_body(), order_headers()
    assert (
        creation_client.post("/api/v1/orders", json=body, headers=headers).status_code
        == 201
    )

    def forbidden(self: OrderSecurity, order: OrderCreate, saved: str) -> bool:
        raise AssertionError("Hidden paths must never compare canonical personal data")

    monkeypatch.setattr(OrderSecurity, "matches_request", forbidden)
    samples: list[list[float]] = []
    queries: list[str] = []

    def collect(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        many: bool,
    ) -> None:
        queries.append(statement)

    for erased in (False, True):
        if erased:
            erase_fixture(creation_db)
        times = []
        event.listen(creation_db.engine, "before_cursor_execute", collect)
        try:
            for _ in range(20):
                start = monotonic()
                response = creation_client.post(
                    "/api/v1/orders",
                    json=body,
                    headers=headers | {"X-Request-Token": "b" * 64},
                )
                times.append((monotonic() - start) * 1000)
                assert response.status_code == 404
        finally:
            event.remove(creation_db.engine, "before_cursor_execute", collect)
        samples.append(times)
    assert all(
        "catalog_items" not in query and "consent_texts" not in query
        for query in queries
    )
    assert len(queries) == 40
    print(
        "Synthetic timing review (ms, no constant-time claim): "
        f"wrong-token={median(samples[0]):.2f}, erased={median(samples[1]):.2f}"
    )


def test_erased_concurrent_matrix_and_deleted_row_boundary(
    creation_client: TestClient,
    creation_db: Database,
) -> None:
    body, headers = support_body(), order_headers()
    first = creation_client.post("/api/v1/orders", json=body, headers=headers)
    assert first.status_code == 201
    erase_fixture(creation_db)
    before = counts(creation_db)

    def retry(index: int) -> int:
        response = creation_client.post(
            "/api/v1/orders",
            json=body if index % 2 else body | {"city": None},
            headers=headers | {"X-Request-Token": "abc"[index % 3] * 64},
        )
        assert response.json()["error"] == {
            "code": "NOT_FOUND",
            "message": "Object not found",
            "details": {},
        }
        return response.status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(retry, range(6))) == [404] * 6
    assert counts(creation_db) == before
    with creation_db.unit_of_work() as session:
        # Normal runtime deletion uses existing CASCADEs; no privileged hook.
        session.execute(delete(Order))
        session.execute(delete(Consent))
    after = creation_client.post("/api/v1/orders", json=body, headers=headers)
    assert after.status_code == 201 and after.json()["number"] != first.json()["number"]
    assert counts(creation_db) == (1, 1, 0, 1, 1)
