"""Real HTTP/DTO/security checks over the isolated PostgreSQL 16 catalog."""

from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.db import Database, DatabaseSettings
from app.main import app
from app.modules.catalog.models import CatalogItem, EquipmentModel
from app.schemas.catalog import (
    CatalogCard,
    CatalogPage,
    EquipmentModelsPage,
)
from app.schemas.catalog import (
    CatalogItem as ItemDTO,
)
from app.schemas.common import ErrorEnvelope

pytestmark = pytest.mark.integration


@pytest.fixture
def catalog_http(
    databases: tuple[Database, Database],
) -> Iterator[tuple[TestClient, dict[str, UUID]]]:
    runtime, migrator = databases
    identifiers: dict[str, UUID] = {}
    with runtime.unit_of_work() as session:
        for status in ("published", "draft", "archived"):
            model = EquipmentModel(
                code=f"TEST-{status}",
                name="Тестовая модель",
                description="Синтетическая модель",
                status=status,
            )
            session.add(model)
            session.flush()
            identifiers[f"model-{status}"] = model.id
        for sku, kind, name, price, status in (
            ("API-001", "product", "Масляный фильтр", Decimal("123.45"), "published"),
            ("API-002", "service", "Обслуживание фильтра", None, "published"),
            ("API-003", "product", "Тестовый комплект", Decimal("0.00"), "published"),
            ("API-004", "service", "Тестовая проверка", Decimal("67.89"), "published"),
            ("API-005", "product", "Масляный фильтр скрытый", None, "draft"),
            ("API-006", "service", "Масляный фильтр архивный", None, "archived"),
            ("API%_\\007", "product", "Спецсимволы", None, "published"),
        ):
            item = CatalogItem(
                sku=sku,
                kind=kind,
                name=name,
                description="Синтетическое описание",
                attributes={"материал": "тест", "nested": {"values": [1, True]}},
                unit="услуга" if kind == "service" else "шт.",
                price_mode="on_request" if price is None else "fixed",
                price=price,
                tax_note="ТЕСТ: налоговый режим не определён",
                requires_shipping=kind == "product",
                image_key="/private/storage/never-public.png",
                status=status,
                revision=7,
            )
            session.add(item)
            session.flush()
            identifiers[sku] = item.id
    try:
        with TestClient(app) as client:
            # Preserve the normal lifespan/configuration and use the real runtime
            # role/database, with no repository/session/SQL mocks or fixtures API.
            yield client, identifiers
    finally:
        with migrator.unit_of_work() as session:
            session.execute(text("DELETE FROM app.catalog_items"))
            session.execute(text("DELETE FROM app.equipment_models"))


def assert_error(client: TestClient, path: str, status: int, code: str) -> None:
    response = client.get(path)
    assert response.status_code == status, response.text
    envelope = ErrorEnvelope.model_validate_json(response.content)
    assert envelope.error.code == code
    assert envelope.error.details == {}
    assert str(envelope.request_id) == response.headers["x-request-id"]
    assert UUID(response.headers["x-request-id"]).version == 4
    assert response.headers["cache-control"] == "no-store"


def test_public_contracts_and_prices(
    catalog_http: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = catalog_http
    response = client.get("/api/v1/catalog/items")
    assert response.status_code == 200
    page = CatalogPage.model_validate_json(response.content)
    assert (page.total, page.limit, page.offset) == (5, 20, 0)
    rows = {row["sku"]: row for row in response.json()["items"]}
    assert rows["API-001"]["price"] == "123.45"
    assert rows["API-002"]["price"] is None
    assert rows["API-002"]["price_mode"] == "on_request"
    assert rows["API-003"]["price"] == "0.00"
    assert rows["API-004"]["price"] == "67.89"
    for sku, row in rows.items():
        detail = client.get(f"/api/v1/catalog/items/{str(ids[sku]).upper()}")
        assert detail.status_code == 200
        dto = ItemDTO.model_validate_json(detail.content)
        assert dto.id == ids[sku]
        assert detail.json()["id"] == str(ids[sku])
        assert {key: value for key, value in detail.json().items() if key in row} == row
        assert set(row) == set(CatalogCard.model_fields)
        assert set(detail.json()) == set(ItemDTO.model_fields)
        assert dto.revision == 7 and dto.currency == "RUB"
        assert dto.tax_note == "ТЕСТ: налоговый режим не определён"
        assert dto.image_url is None and dto.applicability is None
        assert "never-public" not in detail.text
        assert (
            client.get(f"/api/v1/catalog/items/{ids[sku]}/image/key.png").status_code
            == 404
        )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Масляный фильтр", ["API-001"]),
        ("МАСЛЯНЫЙ", ["API-001"]),
        ("фильтр", ["API-001", "API-002"]),
        ("api-001", ["API-001"]),
        ("PI-00", ["API-001", "API-002", "API-003", "API-004"]),
        ("", ["API%_\\007", "API-001", "API-002", "API-003", "API-004"]),
        ("   ", ["API%_\\007", "API-001", "API-002", "API-003", "API-004"]),
        ("нет совпадений", []),
        ("' OR 1=1 --", []),
        ("%", ["API%_\\007"]),
        ("_", ["API%_\\007"]),
        ("\\", ["API%_\\007"]),
        ("%_\\", ["API%_\\007"]),
    ],
)
def test_postgres_literal_search(
    catalog_http: tuple[TestClient, dict[str, UUID]], query: str, expected: list[str]
) -> None:
    client, _ = catalog_http
    response = client.get("/api/v1/catalog/items", params={"q": query})
    assert response.status_code == 200
    # Punctuation order depends on the database locale; test membership here.
    # Repeated requests and page concatenation test stable SQL ordering below.
    assert sorted(row["sku"] for row in response.json()["items"]) == sorted(expected)
    assert response.json()["total"] == len(expected)


def test_filters_pagination_and_total(
    catalog_http: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, _ = catalog_http
    for kind, total in (("product", 3), ("service", 2)):
        path = f"/api/v1/catalog/items?kind={kind}&limit=1&offset=1"
        response = client.get(path)
        assert response.status_code == 200
        page = CatalogPage.model_validate_json(response.content)
        assert (page.total, page.limit, page.offset, len(page.items)) == (
            total,
            1,
            1,
            1,
        )
        assert page.items[0].kind == kind
        assert client.get(path).json() == response.json()
    matched = client.get("/api/v1/catalog/items?q=фильтр&kind=service").json()
    assert matched["total"] == 1 and matched["items"][0]["sku"] == "API-002"
    for resource in ("catalog/items", "equipment-models"):
        response = client.get(f"/api/v1/{resource}?limit=100&offset=10000")
        assert response.status_code == 200
        assert response.json() == {
            "items": [],
            "total": 5 if resource == "catalog/items" else 1,
            "limit": 100,
            "offset": 10000,
        }


def test_publication_and_applicability_boundary(
    catalog_http: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = catalog_http
    models = client.get("/api/v1/equipment-models")
    assert models.status_code == 200
    page = EquipmentModelsPage.model_validate_json(models.content)
    assert (page.total, page.limit, page.offset) == (1, 20, 0)
    assert [row.id for row in page.items] == [ids["model-published"]]
    assert set(models.json()["items"][0]) == {"id", "code", "name", "description"}
    for item_id in (ids["API-005"], ids["API-006"], uuid4()):
        assert_error(client, f"/api/v1/catalog/items/{item_id}", 404, "NOT_FOUND")
    for model_id in (
        ids["model-published"],
        ids["model-draft"],
        ids["model-archived"],
        uuid4(),
    ):
        response = client.get(f"/api/v1/catalog/items?model_id={model_id}&q=фильтр")
        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
        for sku in ("API-001", "API-002"):
            path = f"/api/v1/catalog/items/{ids[sku]}?model_id={model_id}"
            if model_id == ids["model-published"]:
                response = client.get(path)
                assert response.status_code == 200
                assert response.json()["applicability"] == {
                    "model_id": str(model_id),
                    "status": "unknown",
                    "sources": [],
                }
            else:
                assert_error(client, path, 404, "NOT_FOUND")


def test_multiple_pages_have_no_gaps_or_duplicates(
    catalog_http: tuple[TestClient, dict[str, UUID]],
    databases: tuple[Database, Database],
) -> None:
    client, _ = catalog_http
    runtime, _ = databases
    with runtime.unit_of_work() as session:
        for number in range(22):
            session.add(
                EquipmentModel(
                    code=f"PAGE-{number:03}",
                    name="Одинаковое имя",
                    description="Только тест",
                    status="published",
                )
            )
            session.add(
                CatalogItem(
                    sku=f"PAGE-{number:03}",
                    kind="service",
                    name="Одинаковое имя",
                    description="Только тест",
                    unit="услуга",
                    price_mode="on_request",
                    price=None,
                    tax_note="ТЕСТ",
                    status="published",
                )
            )
    for resource, total in (("equipment-models", 23), ("catalog/items", 27)):
        first = client.get(f"/api/v1/{resource}").json()
        second = client.get(f"/api/v1/{resource}?offset=20").json()
        complete = client.get(f"/api/v1/{resource}?limit=100").json()
        assert first["total"] == second["total"] == complete["total"] == total
        assert len(first["items"]) == 20
        assert first["items"] + second["items"] == complete["items"]
        assert len({row["id"] for row in complete["items"]}) == total


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=-1",
        "limit=101",
        "limit=1.5",
        "limit=x",
        "offset=-1",
        "offset=10001",
        "offset=1.5",
        "offset=x",
        "kind=invalid",
        "kind=",
        "model_id=invalid",
        "model_id=",
        "model_id=" + "a" * 32,
        "q=" + "я" * 1001,
        "status=published",
    ],
)
def test_invalid_catalog_parameters(
    catalog_http: tuple[TestClient, dict[str, UUID]], query: str
) -> None:
    client, _ = catalog_http
    assert_error(client, f"/api/v1/catalog/items?{query}", 422, "VALIDATION_ERROR")


def test_validation_models_detail_and_query_boundary(
    catalog_http: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = catalog_http
    for query in ("limit=0", "limit=101", "offset=-1", "offset=10001", "limit=x"):
        assert_error(
            client, f"/api/v1/equipment-models?{query}", 422, "VALIDATION_ERROR"
        )
    for path in (
        "/api/v1/catalog/items/not-a-uuid",
        "/api/v1/catalog/items/" + "a" * 32,
        f"/api/v1/catalog/items/{ids['API-001']}?model_id=invalid",
    ):
        assert_error(client, path, 422, "VALIDATION_ERROR")
    assert (
        client.get("/api/v1/catalog/items", params={"q": "я" * 1000}).status_code == 200
    )


def test_real_database_connection_failure(
    catalog_http: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = catalog_http
    # Real psycopg failure, not a simulated repository exception. Keep the
    # ordinary test PostgreSQL and the application's lifespan database intact.
    unavailable = Database(
        DatabaseSettings("127.0.0.1", 1, "unused", "unused", "unused")
    )
    previous = app.state.database
    app.state.database = unavailable
    try:
        for path in (
            "/api/v1/catalog/items",
            "/api/v1/equipment-models",
            f"/api/v1/catalog/items/{ids['API-001']}",
            f"/api/v1/catalog/items?model_id={ids['model-published']}",
        ):
            assert_error(client, path, 503, "SERVICE_UNAVAILABLE")
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").status_code == 503
    finally:
        app.state.database = previous
        unavailable.close()
    assert client.get("/readyz").status_code == 200
    assert client.get("/api/v1/catalog/items").status_code == 200
