"""HTTP infrastructure/contract checks that need no database credentials."""

import json
from uuid import UUID

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.main import app
from app.schemas.common import ErrorEnvelope


def test_missing_database_has_no_fixture_fallback(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("DB_PASSWORD_FILE", raising=False)
    with TestClient(app) as client:
        for path in ("/api/v1/catalog/items", "/api/v1/equipment-models"):
            response = client.get(path, headers={"X-Request-ID": "untrusted-input"})
            assert response.status_code == 503
            body = ErrorEnvelope.model_validate_json(response.content)
            assert body.error.code == "SERVICE_UNAVAILABLE"
            assert str(body.request_id) == response.headers["x-request-id"]
            assert UUID(response.headers["x-request-id"]).version == 4
            assert "untrusted-input" not in response.text
        response = client.get("/api/v1/not-implemented")
        assert response.status_code == 404
        assert (
            ErrorEnvelope.model_validate_json(response.content).error.code
            == "NOT_FOUND"
        )


def test_actual_openapi_reuses_contracts() -> None:
    with TestClient(app) as client:
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = json.loads(response.content)
    paths = schema["paths"]
    for path, model in (
        ("/api/v1/equipment-models", "EquipmentModelsPage"),
        ("/api/v1/catalog/items", "CatalogPage"),
        ("/api/v1/catalog/items/{id}", "CatalogItem"),
    ):
        operation = paths[path]["get"]
        assert operation.get("security", []) == []
        assert operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ] == {"$ref": f"#/components/schemas/{model}"}
        for status in ("422", "503"):
            assert operation["responses"][status]["content"]["application/json"][
                "schema"
            ] == {"$ref": "#/components/schemas/ErrorEnvelope"}
    parameters = {
        row["name"]: row["schema"]
        for row in paths["/api/v1/catalog/items"]["get"]["parameters"]
    }
    assert parameters["limit"]["default"] == 20
    assert parameters["limit"]["maximum"] == 100
    assert parameters["offset"]["maximum"] == 10000
    assert parameters["q"]["anyOf"][0]["maxLength"] == 1000
    price = schema["components"]["schemas"]["CatalogCard"]["properties"]["price"]
    assert {part["type"] for part in price["anyOf"]} == {"string", "null"}
    assert not any("/image/" in path for path in paths)
