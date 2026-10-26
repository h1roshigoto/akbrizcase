"""MVP-016 real PostgreSQL services, staff forms, rollback and interleavings."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Event
from time import monotonic
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from pydantic import ValidationError
from sqlalchemy import event, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from test_order_reads import ATTACK, ORIGIN, business_rows, create, login
from test_order_reads import read_client as read_client

from app.core.csrf import SESSION_COOKIE
from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff_models import StaffSession, StaffUser
from app.core.staff_sessions import StaffIdentity, resolve_session
from app.main import app
from app.modules.catalog.models import CatalogItem, EquipmentModel
from app.modules.catalog.write_dto import ItemCreate, ItemEdit, ItemState, ModelFields
from app.modules.catalog.write_service import CatalogRejected, CatalogWriteService
from app.modules.orders.service import OrderRejected, create_order, replay_order
from app.schemas.common import OrderHeaders
from app.schemas.orders import OrderCreate

pytestmark = pytest.mark.integration


def fields(**changes: Any) -> dict[str, Any]:
    return {
        "sku": "SYNTHETIC-016",
        "kind": "product",
        "name": "Synthetic item",
        "description": "",
        "attributes": {},
        "unit": "unit",
        "price_mode": "fixed",
        "price": "123.45",
        "currency": "RUB",
        "tax_note": "Synthetic tax note",
        "requires_shipping": False,
        **changes,
    }


def post(client: TestClient, path: str, csrf: str, **values: Any) -> Any:
    return client.post(
        path, data={"csrf_token": csrf, **values}, headers={"Origin": ORIGIN}
    )


def item_form(**changes: Any) -> dict[str, Any]:
    data = fields(**changes)
    data["attributes"] = json.dumps(data["attributes"])
    data["requires_shipping"] = "true" if data["requires_shipping"] else "false"
    if data["price"] is None:
        data["price"] = ""
    return data


def edit_form(revision: int, **changes: Any) -> dict[str, Any]:
    data = item_form(**changes)
    del data["sku"], data["kind"]
    return {**data, "expected_revision": str(revision)}


def snapshots(database: Database) -> dict[str, Any]:
    with database.unit_of_work() as session:
        return {
            table: list(
                session.scalars(
                    text(f"SELECT row_to_json(t) FROM app.{table} t ORDER BY id")
                )
            )
            for table in ("catalog_items", "equipment_models", "audit_events")
        }


def catalog_audit(database: Database) -> list[Any]:
    return [
        row
        for row in snapshots(database)["audit_events"]
        if row["entity_type"] in ("catalog_item", "equipment_model")
    ]


def identity(client: TestClient, database: Database) -> StaffIdentity:
    login(client, "admin")
    token = client.cookies.get(SESSION_COOKIE)
    assert token is not None
    with database.unit_of_work() as session:
        actor = resolve_session(session, token)
        assert actor is not None
        return actor


def test_item_model_lifecycle_and_public_search(
    read_client: TestClient, staff_database: Database
) -> None:
    csrf = login(read_client, "admin")
    model = post(
        read_client,
        "/admin/catalog/models/new",
        csrf,
        code="MVP016",
        name=ATTACK,
        description=ATTACK,
    )
    assert model.status_code == 303
    model_path = model.headers["location"]
    model_id = UUID(model_path.rsplit("/", 1)[1])
    page = read_client.get(model_path)
    escaped_model = page.status_code == 200 and str(escape(ATTACK)) in page.text
    assert escaped_model, "Model fields must be escaped without exposing the form"
    assert not any(
        row["id"] == str(model_id)
        for row in read_client.get("/api/v1/equipment-models").json()["items"]
    )
    with staff_database.unit_of_work() as session:
        row = session.get(EquipmentModel, model_id)
        assert row is not None and row.status == "draft"
        timestamp = row.updated_at.isoformat()
    assert (
        post(
            read_client,
            model_path,
            csrf,
            code="MVP016-EDIT",
            name=ATTACK,
            description="Updated",
            expected_updated_at=timestamp,
        ).status_code
        == 303
    )
    assert (
        post(
            read_client,
            model_path,
            csrf,
            code="MVP016-LOST",
            name="Lost",
            description="",
            expected_updated_at=timestamp,
        ).status_code
        == 409
    )
    with staff_database.unit_of_work() as session:
        timestamp = session.get(EquipmentModel, model_id).updated_at.isoformat()  # type: ignore[union-attr]
    assert (
        post(
            read_client, model_path + "/publish", csrf, expected_updated_at=timestamp
        ).status_code
        == 303
    )
    assert any(
        row["id"] == str(model_id)
        for row in read_client.get("/api/v1/equipment-models").json()["items"]
    )

    for kind in ("product", "service"):
        values = item_form(
            sku=f"MVP016-{kind} № 1",
            kind=kind,
            name=ATTACK,
            description=ATTACK,
            tax_note=ATTACK,
            attributes={"nested": [ATTACK, {"value": ATTACK}]},
            price_mode="fixed" if kind == "product" else "on_request",
            price="0.00" if kind == "product" else None,
        )
        response = post(read_client, "/admin/catalog/items/new", csrf, **values)
        assert response.status_code == 303
        path = response.headers["location"]
        identifier = UUID(path.rsplit("/", 1)[1])
        assert read_client.get(f"/api/v1/catalog/items/{identifier}").status_code == 404
        assert (
            read_client.get(f"/api/v1/catalog/items?q=MVP016-{kind}").json()["total"]
            == 0
        )
        assert (
            post(
                read_client, path + "/publish", csrf, expected_revision="1"
            ).status_code
            == 303
        )
        public = read_client.get(f"/api/v1/catalog/items/{identifier}").json()
        assert public["revision"] == 2 and public["attributes"]["nested"][0] == ATTACK
        assert public["image_url"] is None and public["applicability"] is None
        assert public["price"] == ("0.00" if kind == "product" else None)
        escaped_item = str(escape(ATTACK)) in read_client.get(path).text
        assert escaped_item, "Catalog fields must be escaped"
        edit = edit_form(
            2, name="Synthetic updated name", price="22.22", tax_note=ATTACK
        )
        assert post(read_client, path, csrf, **edit).status_code == 303
        assert (
            read_client.get(f"/api/v1/catalog/items/{identifier}").json()["revision"]
            == 3
        )
        assert post(read_client, path, csrf, **edit).status_code == 409
        assert (
            read_client.get(
                "/api/v1/catalog/items?q=Synthetic%20updated%20name"
            ).json()["total"]
            >= 1
        )
        before = catalog_audit(staff_database)
        assert (
            post(
                read_client, path, csrf, **{**edit, "expected_revision": "3"}
            ).status_code
            == 303
        )
        assert catalog_audit(staff_database) == before  # No-op is not a mutation.
        assert (
            post(
                read_client, path + "/archive", csrf, expected_revision="3"
            ).status_code
            == 303
        )
        assert read_client.get(f"/api/v1/catalog/items/{identifier}").status_code == 404
        assert (
            read_client.get(f"/api/v1/catalog/items?q=MVP016-{kind}").json()["total"]
            == 0
        )
        assert (
            post(
                read_client, path + "/publish", csrf, expected_revision="4"
            ).status_code
            == 409
        )
        assert (
            post(
                read_client, path, csrf, **{**edit, "expected_revision": "4"}
            ).status_code
            == 409
        )
    with staff_database.unit_of_work() as session:
        timestamp = session.get(EquipmentModel, model_id).updated_at.isoformat()  # type: ignore[union-attr]
    assert (
        post(
            read_client, model_path + "/archive", csrf, expected_updated_at=timestamp
        ).status_code
        == 303
    )
    assert not any(
        row["id"] == str(model_id)
        for row in read_client.get("/api/v1/equipment-models").json()["items"]
    )
    audits = catalog_audit(staff_database)
    assert len(audits) == 12
    with staff_database.unit_of_work() as session:
        actor_id = str(
            session.scalar(select(StaffUser.id).where(StaffUser.login == "read-admin"))
        )
    for audit in audits:
        assert audit["actor_id"] == actor_id and audit["result"] == "success"
        assert set(audit["metadata"]) <= {"status", "revision"}
        assert ATTACK not in json.dumps(audit)


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_direct_routes_and_generic_crud(
    read_client: TestClient, staff_database: Database, role: str
) -> None:
    csrf = login(read_client, role)
    before = snapshots(staff_database)
    status = 403 if role == "manager" else 200
    for path in (
        "/admin/catalog-items",
        "/admin/equipment-models",
        "/admin/catalog/items/new",
        "/admin/catalog/models/new",
    ):
        assert read_client.get(path).status_code == status
    if role == "manager":
        assert 'href="/admin/catalog-items"' not in read_client.get("/admin/").text
        for entity in ("items", "models"):
            for path in (
                f"/admin/catalog/{entity}/new",
                f"/admin/catalog/{entity}/{uuid4()}",
                f"/admin/catalog/{entity}/{uuid4()}/publish",
                f"/admin/catalog/{entity}/{uuid4()}/archive",
            ):
                assert read_client.get(path).status_code in (403, 405)
                assert post(read_client, path, csrf).status_code == 403
                assert read_client.patch(
                    path, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}
                ).status_code in (403, 405)
    for model in ("catalog-item", "catalogitem", "equipment-model", "equipmentmodel"):
        for operation in ("create", "edit", "delete", "export", "action"):
            response = post(
                read_client, f"/admin/{model}/{operation}", csrf, id=str(uuid4())
            )
            if response.status_code == 307:
                response = post(
                    read_client, response.headers["location"], csrf, id=str(uuid4())
                )
            assert response.status_code in (404, 405)
    after = snapshots(staff_database)
    assert (
        before["catalog_items"] == after["catalog_items"]
        and before["equipment_models"] == after["equipment_models"]
    )
    assert catalog_audit(staff_database) == []


@pytest.mark.parametrize(
    "session_state", ["anonymous", "forged", "expired", "revoked", "disabled"]
)
def test_session_denial(
    read_client: TestClient, staff_database: Database, session_state: str
) -> None:
    csrf = login(read_client, "admin")
    with staff_database.unit_of_work() as session:
        if session_state == "expired":
            session.execute(
                update(StaffSession).values(expires_at=utc_now() - timedelta(seconds=1))
            )
        elif session_state == "revoked":
            session.execute(update(StaffSession).values(revoked_at=utc_now()))
        elif session_state == "disabled":
            session.execute(update(StaffUser).values(is_active=False))
    if session_state in ("anonymous", "forged"):
        read_client.cookies.clear()
    if session_state == "forged":
        read_client.cookies.set(SESSION_COOKIE, "f" * 64)
    for entity in ("items", "models"):
        path = f"/admin/catalog/{entity}/new"
        assert read_client.get(path).status_code == 401
        assert post(read_client, path, csrf).status_code == 401
    assert catalog_audit(staff_database) == []


def test_csrf_origin_and_pollution(
    read_client: TestClient, staff_database: Database
) -> None:
    csrf = login(read_client, "admin")
    before = snapshots(staff_database)
    for origin in (
        None,
        "",
        "null",
        "https://foreign.invalid",
        "http://localhost:8443",
    ):
        headers = {"X-CSRF-Token": csrf}
        if origin is not None:
            headers["Origin"] = origin
        assert (
            read_client.post(
                "/admin/catalog/items/new", data=item_form(), headers=headers
            ).status_code
            == 403
        )
    for token in (None, "forged"):
        headers = {"Origin": ORIGIN}
        if token is not None:
            headers["X-CSRF-Token"] = token
        assert (
            read_client.post(
                "/admin/catalog/items/new", data=item_form(), headers=headers
            ).status_code
            == 403
        )
    other_csrf = login(read_client, "admin")
    assert other_csrf != csrf
    assert (
        post(read_client, "/admin/catalog/items/new", csrf, **item_form()).status_code
        == 403
    )
    csrf = other_csrf
    polluted = urlencode(
        [("csrf_token", csrf), *item_form().items(), ("name", "overwrite")]
    )
    assert (
        read_client.post(
            "/admin/catalog/items/new",
            content=polluted,
            headers={
                "Origin": ORIGIN,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ).status_code
        == 422
    )
    assert read_client.get("/admin/catalog-items?limit=1&limit=2").status_code == 422
    upload = read_client.post(
        "/admin/catalog/items/new",
        files={"image": ("synthetic.txt", b"synthetic")},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert (
        upload.status_code == 422
        and upload.json()["error"]["code"] == "INVALID_REQUEST"
    )
    deep = '{"nested":' * 1100 + "{}" + "}" * 1100
    assert (
        post(
            read_client,
            f"/admin/catalog/items/{uuid4()}/publish",
            csrf,
            expected_revision="1",
            attributes=deep,
        ).status_code
        == 422
    )
    for changes in (
        {"image_key": "bad"},
        {"id": str(uuid4())},
        {"revision": "50"},
        {"status": "published"},
        {"actor_id": str(uuid4())},
        {"attributes": "[]"},
        {"attributes": '{"a":1,"a":2}'},
        {"attributes": "not-json"},
        {"attributes": '{"a":NaN}'},
        {"price_mode": "on_request", "price": "0"},
        {"price": "1.001"},
        {"kind": "service", "requires_shipping": "true"},
    ):
        response = post(
            read_client, "/admin/catalog/items/new", csrf, **{**item_form(), **changes}
        )
        assert response.status_code == 422
        assert response.headers["cache-control"] == "no-store"
        assert "SQL" not in response.text
    after = snapshots(staff_database)
    assert before["catalog_items"] == after["catalog_items"]
    assert catalog_audit(staff_database) == []


@pytest.mark.parametrize("entity", ["items", "models"])
def test_unique_missing_and_stale_model_item(
    read_client: TestClient, staff_database: Database, entity: str
) -> None:
    csrf = login(read_client, "admin")
    values = (
        item_form()
        if entity == "items"
        else {"code": "MVP016", "name": "Synthetic", "description": ""}
    )
    response = post(read_client, f"/admin/catalog/{entity}/new", csrf, **values)
    assert response.status_code == 303
    before = snapshots(staff_database)
    assert (
        post(read_client, f"/admin/catalog/{entity}/new", csrf, **values).status_code
        == 409
    )
    assert snapshots(staff_database) == before
    path = f"/admin/catalog/{entity}/{uuid4()}"
    assert read_client.get(path).status_code == 404
    state = (
        {"expected_revision": "1"}
        if entity == "items"
        else {"expected_updated_at": utc_now().isoformat()}
    )
    assert post(read_client, path + "/publish", csrf, **state).status_code == 404
    assert (
        post(
            read_client, response.headers["location"] + "/archive", csrf, **state
        ).status_code
        == 409
    )


@pytest.mark.parametrize("entity", ["items", "models"])
@pytest.mark.parametrize("operation", ["create", "edit", "publish", "archive"])
@pytest.mark.parametrize("failure", ["audit", "commit"])
def test_mutation_atomicity(
    read_client: TestClient,
    staff_database: Database,
    entity: str,
    operation: str,
    failure: str,
) -> None:
    csrf = login(read_client, "admin")
    values = (
        item_form()
        if entity == "items"
        else {"code": "MVP016", "name": "Synthetic", "description": ""}
    )
    path = f"/admin/catalog/{entity}/new"
    if operation != "create":
        result = post(read_client, path, csrf, **values)
        assert result.status_code == 303
        path = result.headers["location"]
        if operation == "archive":
            if entity == "items":
                state = {"expected_revision": "1"}
            else:
                with staff_database.unit_of_work() as session:
                    stored_model = session.get(
                        EquipmentModel, UUID(path.rsplit("/", 1)[1])
                    )
                    assert stored_model is not None
                    state = {"expected_updated_at": stored_model.updated_at.isoformat()}
            assert (
                post(read_client, path + "/publish", csrf, **state).status_code == 303
            )
        with staff_database.unit_of_work() as session:
            if entity == "items":
                row = session.get(CatalogItem, UUID(path.rsplit("/", 1)[1]))
                assert row is not None
                state = {"expected_revision": str(row.revision)}
            else:
                model = session.get(EquipmentModel, UUID(path.rsplit("/", 1)[1]))
                assert model is not None
                state = {"expected_updated_at": model.updated_at.isoformat()}
        if operation == "edit":
            values = (
                edit_form(int(state["expected_revision"]), name="Changed")
                if entity == "items"
                else {**values, **state, "name": "Changed"}
            )
        else:
            path += "/" + operation
            values = state
    before = snapshots(staff_database)
    hits = []

    def fail_audit(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> tuple[str, Any]:
        if statement.startswith("INSERT INTO app.audit_events"):
            hits.append(True)
            return "SELECT 1/0", {}
        return statement, parameters

    def fail_commit(connection: Connection) -> None:
        hits.append(True)
        connection.exec_driver_sql("SELECT 1/0")

    callback: Any = fail_audit if failure == "audit" else fail_commit
    hook = "before_cursor_execute" if failure == "audit" else "commit"
    event.listen(
        staff_database.engine,
        hook,
        callback,
        **({"retval": True} if failure == "audit" else {}),
    )
    try:
        result = post(read_client, path, csrf, **values)
        assert result.status_code == 503 and "1/0" not in result.text
    finally:
        event.remove(staff_database.engine, hook, callback)
    assert hits == [True]
    assert snapshots(staff_database) == before, "Partial catalog or audit commit"


def observe_block(session: Session, waiter_pid: int) -> None:
    deadline = monotonic() + 2
    tick = Event()
    while monotonic() < deadline:
        session.execute(text("SELECT pg_stat_clear_snapshot()"))
        if session.scalar(
            text(
                "SELECT wait_event_type='Lock' AND "
                "pg_backend_pid() = ANY(pg_blocking_pids(pid)) "
                "FROM pg_stat_activity WHERE pid=:pid"
            ),
            {"pid": waiter_pid},
        ):
            return
        tick.wait(0.01)
    pytest.fail("Independent PostgreSQL transaction did not wait on the winner")


@pytest.mark.parametrize("entity", ["items", "models"])
def test_concurrent_unique_creation(
    read_client: TestClient, staff_database: Database, entity: str
) -> None:
    actor = identity(read_client, staff_database)
    ready, inserted = Barrier(2), Event()
    pids: dict[bool, int] = {}

    def attempt(first: bool) -> int:
        with staff_database.unit_of_work() as session:
            pids[first] = session.scalar(text("SELECT pg_backend_pid()"))
            ready.wait(timeout=5)
            if not first:
                assert inserted.wait(timeout=5)
            try:
                service = CatalogWriteService(session, actor)
                if entity == "items":
                    service.create_item(ItemCreate.model_validate(fields()))
                else:
                    service.create_model(
                        ModelFields(code="UNIQUE-016", name="Synthetic", description="")
                    )
                if first:
                    inserted.set()
                    observe_block(session, pids[False])
            except CatalogRejected as error:
                # The losing SAVEPOINT rolled back; outer transaction is usable.
                assert session.scalar(text("SELECT 1")) == 1
                return error.status
        return 200

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, first) for first in (True, False)]
        results = [future.result(timeout=10) for future in futures]
    assert len(set(pids.values())) == 2 and sorted(results) == [200, 409]
    assert len(catalog_audit(staff_database)) == 1


@pytest.mark.parametrize("entity", ["items", "models"])
def test_publication_revalidates_locked_data(
    read_client: TestClient, staff_database: Database, entity: str
) -> None:
    csrf = login(read_client, "admin")
    values = (
        item_form()
        if entity == "items"
        else {"code": "INVALID-016", "name": "Synthetic", "description": ""}
    )
    response = post(read_client, f"/admin/catalog/{entity}/new", csrf, **values)
    assert response.status_code == 303
    path = response.headers["location"]
    identifier = UUID(path.rsplit("/", 1)[1])
    with staff_database.unit_of_work() as session:
        if entity == "items":
            session.execute(
                update(CatalogItem).where(CatalogItem.id == identifier).values(name=" ")
            )
            state = {"expected_revision": "1"}
        else:
            model = session.get(EquipmentModel, identifier)
            assert model is not None
            model.name = " "
            session.flush()
            state = {"expected_updated_at": model.updated_at.isoformat()}
    before = snapshots(staff_database)
    assert post(read_client, path + "/publish", csrf, **state).status_code == 422
    assert snapshots(staff_database) == before


def test_service_rejects_manager(
    read_client: TestClient, staff_database: Database
) -> None:
    login(read_client, "manager")
    token = read_client.cookies.get(SESSION_COOKIE)
    assert token is not None
    with staff_database.unit_of_work() as session:
        actor = resolve_session(session, token)
        assert actor is not None
        with pytest.raises(CatalogRejected) as rejected:
            CatalogWriteService(session, actor)
        assert rejected.value.status == 403
    assert catalog_audit(staff_database) == []


@pytest.mark.parametrize(
    "price", [Decimal("1.001"), Decimal("NaN"), Decimal("10000000000.00")]
)
def test_service_revalidates_original_money(
    read_client: TestClient, staff_database: Database, price: Decimal
) -> None:
    actor = identity(read_client, staff_database)
    command = ItemCreate.model_validate(fields()).model_copy(update={"price": price})
    before = snapshots(staff_database)
    with staff_database.unit_of_work() as session:
        with pytest.raises(ValidationError):
            CatalogWriteService(session, actor).create_item(command)
    assert snapshots(staff_database) == before


def test_json_boolean_number_change_and_key_order_noop(
    read_client: TestClient, staff_database: Database
) -> None:
    csrf = login(read_client, "admin")
    response = post(
        read_client,
        "/admin/catalog/items/new",
        csrf,
        **item_form(attributes={"first": True, "second": {"value": 1}}),
    )
    assert response.status_code == 303
    path = response.headers["location"]
    assert (
        post(
            read_client,
            path,
            csrf,
            **edit_form(1, attributes={"first": 1, "second": {"value": 1}}),
        ).status_code
        == 303
    )
    before = snapshots(staff_database)
    assert any(
        row["metadata"].get("revision") == 2 for row in catalog_audit(staff_database)
    )
    stored = next(
        row for row in before["catalog_items"] if row["sku"] == "SYNTHETIC-016"
    )
    assert type(stored["attributes"]["first"]) is int
    assert (
        post(
            read_client,
            path,
            csrf,
            **edit_form(2, attributes={"second": {"value": 1}, "first": 1}),
        ).status_code
        == 303
    )
    assert snapshots(staff_database) == before


@pytest.mark.parametrize("entity", ["items", "models"])
def test_concurrent_edits_refresh_preloaded_identity(
    read_client: TestClient, staff_database: Database, entity: str
) -> None:
    actor = identity(read_client, staff_database)
    with staff_database.unit_of_work() as session:
        service = CatalogWriteService(session, actor)
        row = (
            service.create_item(ItemCreate.model_validate(fields()))
            if entity == "items"
            else service.create_model(
                ModelFields(code="MVP016", name="Original", description="")
            )
        )
        identifier, timestamp = row.id, row.updated_at
    ready, locked = Barrier(2), Event()
    pids: dict[bool, int] = {}

    def attempt(first: bool) -> int:
        with staff_database.unit_of_work() as session:
            pids[first] = session.scalar(text("SELECT pg_backend_pid()"))
            loaded = session.get(
                CatalogItem if entity == "items" else EquipmentModel, identifier
            )
            assert loaded is not None
            ready.wait(timeout=5)
            if first:
                session.execute(
                    select(CatalogItem if entity == "items" else EquipmentModel)
                    .where(
                        (CatalogItem.id if entity == "items" else EquipmentModel.id)
                        == identifier
                    )
                    .with_for_update()
                )
                locked.set()
            else:
                assert locked.wait(timeout=5)
            try:
                service = CatalogWriteService(session, actor)
                if entity == "items":
                    values = fields(name="Winner" if first else "Lost")
                    del values["sku"], values["kind"]
                    service.edit_item(
                        identifier,
                        ItemEdit.model_validate({**values, "expected_revision": 1}),
                    )
                else:
                    from app.modules.catalog.write_dto import ModelEdit

                    service.edit_model(
                        identifier,
                        ModelEdit(
                            code="MVP016",
                            name="Winner" if first else "Lost",
                            description="",
                            expected_updated_at=timestamp,
                        ),
                    )
                if first:
                    observe_block(session, pids[False])
            except CatalogRejected as error:
                return error.status
        return 200

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, first) for first in (True, False)]
        results = [future.result(timeout=10) for future in futures]
    assert len(set(pids.values())) == 2 and sorted(results) == [200, 409]
    assert len(catalog_audit(staff_database)) == 2
    with staff_database.unit_of_work() as session:
        result = (
            session.get(CatalogItem, identifier)
            if entity == "items"
            else session.get(EquipmentModel, identifier)
        )
        assert result is not None and result.name == "Winner"


@pytest.mark.parametrize("admin_first", [True, False])
@pytest.mark.parametrize("action", ["edit", "archive"])
def test_order_creation_catalog_lock_interleavings(
    read_client: TestClient, staff_database: Database, admin_first: bool, action: str
) -> None:
    actor = identity(read_client, staff_database)
    with staff_database.unit_of_work() as session:
        product = session.scalar(
            select(CatalogItem).where(CatalogItem.sku == "DEMO-P-001")
        )
        assert product is not None
        identifier = product.id
    headers = OrderHeaders.model_validate(
        {"Idempotency-Key": str(uuid4()), "X-Request-Token": "d" * 64}
    )
    request = OrderCreate.model_validate(
        {
            "kind": "commercial",
            "source": "site",
            "confirmed": True,
            "items": [{"item_id": str(identifier), "qty": 2, "expected_revision": 1}],
            "contact": {"name": "Synthetic race", "email": "race@example.invalid"},
            "consent": {"version": "SYNTHETIC-DEMO-ONLY-v1", "accepted": True},
        }
    )
    ready, locked, committed = Barrier(2), Event(), Event()
    pids: dict[str, int] = {}
    commits: list[str] = []

    def run(who: str) -> Any:
        first = (who == "admin") == admin_first
        result: Any = None
        try:
            with staff_database.unit_of_work() as session:
                pids[who] = session.scalar(text("SELECT pg_backend_pid()"))
                loaded = session.get(CatalogItem, identifier)
                assert loaded is not None and loaded.revision == 1
                ready.wait(timeout=5)
                if first:
                    session.execute(
                        select(CatalogItem.id)
                        .where(CatalogItem.id == identifier)
                        .with_for_update()
                    )
                    locked.set()
                else:
                    assert locked.wait(timeout=5)
                if who == "admin":
                    service = CatalogWriteService(session, actor)
                    if action == "archive":
                        service.change_item_state(
                            identifier, ItemState(expected_revision=1), "archive"
                        )
                    else:
                        values = fields(
                            name="Updated after snapshot",
                            tax_note="Changed tax",
                            price="999.99",
                            requires_shipping=False,
                        )
                        del values["sku"], values["kind"]
                        service.edit_item(
                            identifier,
                            ItemEdit.model_validate({**values, "expected_revision": 1}),
                        )
                    result = 200
                else:
                    result = create_order(
                        session, request, headers, app.state.order_security
                    )
                if first:
                    observe_block(session, pids["order" if who == "admin" else "admin"])
            if first:
                commits.append(who)
                committed.set()
            else:
                assert committed.wait(timeout=5)
                commits.append(who)
        except OrderRejected as error:
            assert committed.wait(timeout=5)
            return error.status, error.code
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        admin_future, order_future = (
            pool.submit(run, "admin"),
            pool.submit(run, "order"),
        )
        assert admin_future.result(timeout=10) == 200
        order_result = order_future.result(timeout=10)
    assert len(set(pids.values())) == 2
    if admin_first:
        assert commits == ["admin"]
        assert order_result == (409, "CATALOG_CHANGED")
        assert all(not rows for rows in business_rows(staff_database).values())
    else:
        assert commits == ["order", "admin"]
        before = business_rows(staff_database)
        snapshot = before["order_items"][0]
        assert snapshot["name_snapshot"] != "Updated after snapshot"
        assert snapshot["sku_snapshot"] == "DEMO-P-001" and snapshot["qty"] == 2
        assert (
            snapshot["unit_price_snapshot"] == 123.45
            and snapshot["catalog_revision_snapshot"] == 1
        )
        assert snapshot["requires_shipping_snapshot"] is True
        assert before["orders"][0]["known_subtotal"] == 246.90
        with staff_database.unit_of_work() as session:
            assert (
                replay_order(session, request, headers, app.state.order_security)
                == order_result
            )
        assert business_rows(staff_database) == before, "Replay changed immutable rows"


def test_all_order_rows_survive_commercial_edits(
    read_client: TestClient, staff_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    original = read_client.post

    def capture(url: str, **kwargs: Any) -> Any:
        if url == "/api/v1/orders":
            captured.update(kwargs)
        return original(url, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(read_client, "post", capture)
        order_id = create(read_client, staff_database)
    receipt = original("/api/v1/orders", **captured).json()
    before = business_rows(staff_database)
    csrf = login(read_client, "admin")
    with staff_database.unit_of_work() as session:
        items = list(
            session.scalars(
                select(CatalogItem).where(
                    CatalogItem.sku.in_(["DEMO-P-001", "DEMO-S-001"])
                )
            )
        )
    for row in items:
        path = f"/admin/catalog/items/{row.id}"
        edit = edit_form(
            1,
            name="Synthetic renamed",
            price="567.89",
            tax_note="Synthetic changed tax",
            requires_shipping=False,
        )
        assert post(read_client, path, csrf, **edit).status_code == 303
        assert business_rows(staff_database) == before
        assert (
            post(
                read_client, path + "/archive", csrf, expected_revision="2"
            ).status_code
            == 303
        )
        assert business_rows(staff_database) == before
        assert read_client.get(f"/api/v1/admin/orders/{order_id}").status_code == 200
        replay = original("/api/v1/orders", **captured)
        assert replay.status_code == 200 and replay.json() == receipt
        assert business_rows(staff_database) == before
