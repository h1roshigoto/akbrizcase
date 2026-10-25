"""Real PostgreSQL staff projections, HTTP denial and literal HTML rendering."""

import html
import secrets
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.engine import Connection

from app.core.csrf import SESSION_COOKIE, StaffSecurity
from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff import bootstrap_admin, create_staff
from app.core.staff_models import StaffSession, StaffUser
from app.main import admin, app
from app.modules.catalog.models import CatalogItem, EquipmentModel
from app.modules.catalog.seed import seed_catalog
from app.modules.notify.models import EmailOutbox
from app.modules.orders.models import Consent, ConsentText, Order, OrderStatusHistory
from app.modules.orders.read_service import OrderReadService
from app.modules.orders.security import OrderSecurity
from app.modules.orders.seed import DEMO_TEXT, DEMO_VERSION, seed_consent
from app.schemas.order_reads import OrderDetail, OrderPage

pytestmark = pytest.mark.integration
ORIGIN = "https://localhost:8443"
PASSWORD = "synthetic-order-read-password"
ATTACK = (
    '<script>window.orderInjected=true</script><img src=x onerror="alert(1)"> & \' "'
)


@pytest.fixture
def read_client(
    databases: tuple[Database, Database],
    staff_database: Database,
) -> Iterator[TestClient]:
    runtime, migrator = databases
    with runtime.unit_of_work() as session:
        session.execute(update(ConsentText).values(retired_at=utc_now()))
    seed_consent(runtime, mode="test")
    seed_catalog(runtime, mode="test")
    with runtime.unit_of_work() as session:
        session.execute(
            update(ConsentText)
            .where(ConsentText.version == DEMO_VERSION)
            .values(retired_at=None)
        )
        session.execute(
            update(CatalogItem)
            .where(CatalogItem.sku == "DEMO-S-001")
            .values(price_mode="on_request", price=None)
        )
        assert bootstrap_admin(session, login="read-admin", password=PASSWORD).ok
        assert create_staff(
            session,
            admin_login="read-admin",
            admin_password=PASSWORD,
            login="read-manager",
            password=PASSWORD,
            role="manager",
        ).ok
    try:
        with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
            app.state.database = runtime
            app.state.staff_security = StaffSecurity(
                frozenset({ORIGIN}), secrets.token_hex(32)
            )
            app.state.order_security = OrderSecurity(b"synthetic-order-read-key" * 2)
            yield client
    finally:
        # Orders/history referencing staff must be removed before staff cleanup.
        with migrator.unit_of_work() as session:
            session.execute(delete(Order))
            session.execute(delete(Consent))
            session.execute(delete(CatalogItem))
            session.execute(delete(EquipmentModel))
            session.execute(update(ConsentText).values(retired_at=utc_now()))


def login(client: TestClient, role: str = "manager") -> str:
    client.cookies.clear()
    csrf = client.get("/admin/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/admin/auth/login",
        json={"login": f"read-{role}", "password": PASSWORD},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def create(client: TestClient, database: Database, kind: str = "commercial") -> UUID:
    with database.unit_of_work() as session:
        items = list(
            session.scalars(
                select(CatalogItem)
                .where(CatalogItem.sku.in_(("DEMO-P-001", "DEMO-S-001")))
                .order_by(CatalogItem.sku)
            )
        )
        body: dict[str, Any] = {
            "kind": kind,
            "items": [
                {"item_id": str(row.id), "qty": 2, "expected_revision": row.revision}
                for row in items
            ]
            if kind == "commercial"
            else [],
            "contact": {
                "name": ATTACK,
                "email": "synthetic@example.invalid",
                "phone": "+7 000 000 00 00",
            },
            "organization_name": ATTACK,
            "city": ATTACK,
            "comment": ATTACK,
            "source": "site",
            "confirmed": True,
            "consent": {"version": DEMO_VERSION, "accepted": True},
        }
    result = client.post(
        "/api/v1/orders",
        json=body,
        headers={
            "Idempotency-Key": str(uuid4()),
            "X-Request-Token": "c" * 64,
        },
    )
    assert result.status_code == 201, result.text
    with database.unit_of_work() as session:
        identifier = session.scalar(
            select(Order.id).where(Order.number == result.json()["number"])
        )
        assert identifier is not None
        return identifier


def routes(identifier: UUID) -> tuple[str, ...]:
    return (
        "/admin/orders",
        f"/admin/orders/{identifier}",
        "/api/v1/admin/orders",
        f"/api/v1/admin/orders/{identifier}",
    )


def business_rows(database: Database) -> dict[str, Any]:
    # Private test-only snapshot, never printed or returned by application code.
    with database.unit_of_work() as session:
        return {
            table: session.execute(
                text(f"SELECT row_to_json(t) FROM app.{table} t ORDER BY id")
            )
            .scalars()
            .all()
            for table in (
                "orders",
                "consents",
                "order_items",
                "order_status_history",
                "email_outbox",
            )
        }


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_persisted_detail_snapshots_consent_history_and_no_get_writes(
    read_client: TestClient,
    staff_database: Database,
    role: str,
) -> None:
    identifier = create(read_client, staff_database)
    support = create(read_client, staff_database, "support")
    login(read_client, role)
    with staff_database.unit_of_work() as session:
        original = OrderReadService(session).detail(identifier)
        actor = session.scalar(select(StaffUser.id).where(StaffUser.role == "admin"))
        session.execute(
            update(Order)
            .where(Order.id == identifier)
            .values(status="in_progress", version=2)
        )
        session.add(
            OrderStatusHistory(
                order_id=identifier,
                from_status="new",
                to_status="in_progress",
                changed_by=actor,
                reason=ATTACK,
            )
        )
        session.execute(
            update(CatalogItem).values(
                name="CHANGED CURRENT CATALOG",
                revision=9,
            )
        )
        session.execute(
            update(CatalogItem)
            .where(CatalogItem.price_mode == "fixed")
            .values(price=Decimal("999.99"))
        )
    before = business_rows(staff_database)
    response = read_client.get(f"/api/v1/admin/orders/{identifier}")
    assert response.status_code == 200, response.text
    result = OrderDetail.model_validate_json(response.content)
    assert result.items == original.items
    assert {item.kind_snapshot for item in result.items} == {"product", "service"}
    assert result.known_subtotal == Decimal("246.90")
    assert result.items_total is result.delivery_cost is result.total_amount is None
    assert result.notification_status == "pending"  # No SMTP worker exists.
    assert result.delivery_status == "pending_quote"
    assert result.consent.id == original.consent.id
    assert result.consent.text_version == DEMO_VERSION
    assert result.consent.granted_at == original.consent.granted_at
    assert [(row.from_status, row.to_status) for row in result.history] == [
        (None, "new"),
        ("new", "in_progress"),
    ]
    assert result.history[1].changed_by == actor
    for order_id in (identifier, support):
        page = read_client.get(f"/admin/orders/{order_id}")
        assert page.status_code == 200, page.text
        literal_present = ATTACK in html.unescape(page.text)
        assert literal_present
        assert ATTACK not in page.text and "<img src=x" not in page.text
        assert "CHANGED CURRENT CATALOG" not in page.text
        assert DEMO_VERSION in page.text and "System" in page.text
        assert "Requires quotation" in page.text or "Not applicable" in page.text
        assert "window.orderInjected" not in page.text.split("<script>")[-1]
    support_body = read_client.get(f"/api/v1/admin/orders/{support}").json()
    assert support_body["comment"] == ATTACK and support_body["items"] == []
    listing = read_client.get("/admin/orders")
    assert listing.status_code == 200 and html.escape(ATTACK) not in listing.text
    assert "Current status" in listing.text
    assert read_client.get("/api/v1/admin/orders").status_code == 200
    assert business_rows(staff_database) == before
    hidden = (
        "request_hash",
        "receipt_token_hash",
        "idempotency_key",
        "lease_token",
        "password_hash",
        "token_hash",
        "Idempotency-Key",
        "X-Request-Token",
    )
    with staff_database.unit_of_work() as session:
        order = session.get(Order, identifier)
        assert order is not None
        secret_values = (
            order.request_hash,
            order.receipt_token_hash,
            str(order.idempotency_key),
            "c" * 64,
        )
    for output in (response.text, page.text, listing.text):
        for secret in (*hidden, *secret_values, DEMO_TEXT):
            assert secret and secret not in output


def test_bounded_deterministic_list_filter_and_empty(
    read_client: TestClient,
    staff_database: Database,
) -> None:
    login(read_client)
    assert read_client.get("/api/v1/admin/orders").json() == {
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
    }
    assert "No orders found" in read_client.get("/admin/orders").text
    identifiers = [create(read_client, staff_database) for _ in range(4)]
    with staff_database.unit_of_work() as session:
        session.execute(update(Order).values(created_at=utc_now()))
        session.execute(
            update(Order).where(Order.id == identifiers[0]).values(status="cancelled")
        )
    seen = []
    for offset in range(3):
        response = read_client.get(
            f"/api/v1/admin/orders?status=new&limit=1&offset={offset}"
        )
        page = OrderPage.model_validate_json(response.content)
        assert (page.total, page.limit, page.offset) == (3, 1, offset)
        seen.append(page.items[0].id)
    assert seen == identifiers[:0:-1]
    assert (
        read_client.get("/api/v1/admin/orders?offset=10000&limit=100").json()["items"]
        == []
    )
    assert read_client.get("/api/v1/admin/orders?status=cancelled").json()["total"] == 1
    for status in ("closed", "in_progress"):
        assert (
            read_client.get(f"/api/v1/admin/orders?status={status}").json()["total"]
            == 0
        )
    listing = read_client.get("/admin/orders?status=new&limit=1")
    assert (
        'aria-label="Order pagination"' in listing.text and ">Next</a>" in listing.text
    )
    assert "status=new" in listing.text
    for query in (
        "limit=0",
        "limit=101",
        "limit=x",
        "offset=-1",
        "offset=10001",
        "status=bad",
        "status=",
        "unknown=secret-value",
    ):
        for prefix in ("/admin/orders", "/api/v1/admin/orders"):
            response = read_client.get(f"{prefix}?{query}")
            assert response.status_code == 422, response.text
            assert response.headers["cache-control"] == "no-store"
            assert "secret-value" not in response.text


def test_absent_outbox_erased_pii_and_missing_ids(
    read_client: TestClient,
    staff_database: Database,
) -> None:
    identifier = create(read_client, staff_database, "support")
    login(read_client)
    with staff_database.unit_of_work() as session:
        session.execute(delete(EmailOutbox).where(EmailOutbox.order_id == identifier))
    assert (
        read_client.get(f"/api/v1/admin/orders/{identifier}").json()[
            "notification_status"
        ]
        is None
    )
    assert (
        "No retained notification record"
        in read_client.get(f"/admin/orders/{identifier}").text
    )
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
        session.execute(
            update(OrderStatusHistory)
            .where(OrderStatusHistory.order_id == identifier)
            .values(reason=None)
        )
    before = business_rows(staff_database)
    result = read_client.get(f"/api/v1/admin/orders/{identifier}").json()
    for field in (
        "contact_name",
        "email",
        "phone",
        "organization_name",
        "city",
        "comment",
    ):
        assert result[field] is None
    assert result["consent"]["text_version"] == DEMO_VERSION
    assert result["history"][0]["reason"] is None
    page = read_client.get(f"/admin/orders/{identifier}")
    assert "Personal data erased" in page.text and html.escape(ATTACK) not in page.text
    for prefix in ("/admin/orders", "/api/v1/admin/orders"):
        for suffix, status in ((str(uuid4()), 404), ("malformed", 422)):
            response = read_client.get(f"{prefix}/{suffix}")
            assert response.status_code == status
            assert response.headers["cache-control"] == "no-store"
    assert business_rows(staff_database) == before


@pytest.mark.parametrize(
    "state", ["anonymous", "invalid", "expired", "revoked", "disabled"]
)
def test_session_denial_on_every_order_route(
    read_client: TestClient,
    staff_database: Database,
    state: str,
) -> None:
    identifier = create(read_client, staff_database)
    if state != "anonymous":
        login(read_client)
    if state == "invalid":
        read_client.cookies.clear()
        read_client.cookies.set(SESSION_COOKIE, "f" * 64)
    with staff_database.unit_of_work() as session:
        if state == "expired":
            session.execute(
                update(StaffSession).values(expires_at=utc_now() - timedelta(seconds=1))
            )
        if state == "revoked":
            session.execute(update(StaffSession).values(revoked_at=utc_now()))
        if state == "disabled":
            session.execute(
                update(StaffUser)
                .where(StaffUser.role == "manager")
                .values(is_active=False)
            )
    for path in (*routes(identifier), "/admin/order/list", "/admin/order/create"):
        response = read_client.get(
            path, headers={"X-Staff-Role": "admin", "X-User-ID": str(uuid4())}
        )
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
        assert "synthetic@example.invalid" not in response.text


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_no_generic_crud_export_or_order_mutation(
    read_client: TestClient,
    staff_database: Database,
    role: str,
) -> None:
    identifier = create(read_client, staff_database)
    csrf = login(read_client, role)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    before = business_rows(staff_database)
    assert all(not view.is_model for view in admin.views)
    for model in (
        "order",
        "consent",
        "order-item",
        "order-status-history",
        "email-outbox",
        "consent-text",
    ):
        for action in (
            "list",
            "create",
            "edit",
            "delete",
            "details",
            "export/csv",
            "action/delete",
            "action/bulk-delete",
        ):
            path = f"/admin/{model}/{action}"
            for method in ("GET", "POST", "PATCH", "DELETE"):
                response = read_client.request(
                    method, path, headers=headers, follow_redirects=True
                )
                assert response.status_code in (404, 405), (method, path, response.text)
                assert response.headers["cache-control"] == "no-store"
    for path in routes(identifier):
        for method in ("POST", "PATCH", "DELETE"):
            response = read_client.request(
                method, path, headers=headers, json={"status": "closed"}
            )
            assert response.status_code == 405, (method, path, response.text)
            assert response.headers["cache-control"] == "no-store"
    for prefix in ("/admin/orders", "/api/v1/admin/orders"):
        for action in ("retry-notification",):
            assert read_client.post(
                f"{prefix}/{identifier}/{action}", headers=headers
            ).status_code in (404, 405)
    # Only the narrow SQLAdmin POST is now allowed, and still needs valid input.
    assert (
        read_client.post(
            f"/admin/orders/{identifier}/status", headers=headers
        ).status_code
        == 422
    )
    assert (
        read_client.post(
            f"/api/v1/admin/orders/{identifier}/status", headers=headers
        ).status_code
        == 405
    )
    assert read_client.get(f"/api/v1/orders/{identifier}").status_code == 404
    assert business_rows(staff_database) == before


def test_current_role_and_no_duplicate_session_validation(
    read_client: TestClient,
    staff_database: Database,
) -> None:
    identifier = create(read_client, staff_database)
    login(read_client, "admin")
    with staff_database.unit_of_work() as session:
        session.execute(
            update(StaffUser).where(StaffUser.role == "admin").values(role="manager")
        )
    statements: list[str] = []

    def capture(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(staff_database.engine, "before_cursor_execute", capture)
    try:
        for path in routes(identifier):
            statements.clear()
            assert read_client.get(path).status_code == 200
            assert sum("JOIN app.staff_sessions" in sql for sql in statements) == 1
            business_sql = [
                sql
                for sql in statements
                if any(
                    f"FROM app.{table}" in sql
                    for table in (
                        "orders",
                        "consents",
                        "order_items",
                        "order_status_history",
                        "email_outbox",
                    )
                )
            ]
            assert len(business_sql) == (5 if path.endswith(str(identifier)) else 1)
            for sql in business_sql:
                for forbidden in (
                    "request_hash",
                    "receipt_token_hash",
                    "idempotency_key",
                    "lease_token",
                ):
                    assert forbidden not in sql
                assert "catalog_items" not in sql
    finally:
        event.remove(staff_database.engine, "before_cursor_execute", capture)
