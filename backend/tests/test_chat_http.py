"""Transport contracts without a DB; PostgreSQL acceptance lives in integration."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.main import app


@pytest.fixture
def client(monkeypatch: MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.delenv("DB_PASSWORD_FILE", raising=False)
    with TestClient(app) as browser:
        yield browser


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"mode": "handoff"},
        {"mode": None},
        {"mode": 1},
        {"mode": "diagnose", "session_token": "SYNTHETIC-SECRET"},
        {"mode": "knowledge", "state_version": 1},
        {"mode": "commerce", "owner_token_hash": "SYNTHETIC-SECRET"},
    ],
)
def test_creation_validation(client: TestClient, body: dict[str, object]) -> None:
    response = client.post("/api/v1/chat/sessions", json=body)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["details"] == {}
    assert "SYNTHETIC-SECRET" not in response.text


def test_unavailable_database_and_no_message_endpoint(client: TestClient) -> None:
    for response in (
        client.post("/api/v1/chat/sessions", json={"mode": "diagnose"}),
        client.get(f"/api/v1/chat/sessions/{uuid4()}"),
    ):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert response.headers["cache-control"] == "no-store"
    assert client.post("/api/v1/chat/messages", json={}).status_code == 404


def test_openapi_uses_header_and_bounded_read_contract(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/chat/sessions/{identifier}"]["get"]
    assert {(p["name"], p["in"]) for p in operation["parameters"]} == {
        ("identifier", "path"),
        ("before", "query"),
        ("X-Chat-Token", "header"),
    }
    read = schema["components"]["schemas"]["ChatSessionRead"]["properties"]
    assert read["messages"]["maxItems"] == 50
    assert "session_token" not in read and "owner_token_hash" not in read
