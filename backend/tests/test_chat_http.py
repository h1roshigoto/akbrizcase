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


def test_unavailable_database(client: TestClient) -> None:
    for response in (
        client.post("/api/v1/chat/sessions", json={"mode": "diagnose"}),
        client.get(f"/api/v1/chat/sessions/{uuid4()}"),
        client.post(
            "/api/v1/chat/messages",
            json={
                "session_id": str(uuid4()),
                "request_id": str(uuid4()),
                "expected_version": 0,
                "input": {"type": "text", "value": "SYNTHETIC"},
            },
        ),
    ):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert response.headers["cache-control"] == "no-store"
    assert client.post("/api/v1/chat/messages", json={}).status_code == 422


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
    post = schema["paths"]["/api/v1/chat/messages"]["post"]
    assert {(p["name"], p["in"]) for p in post["parameters"]} == {
        ("X-Chat-Token", "header")
    }
    assert post["requestBody"]["content"]["application/json"]["schema"]["required"] == [
        "session_id",
        "request_id",
        "expected_version",
        "input",
    ]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"session_id":"one","session_id":"two"}',
        b'{"input":{"type":"text","type":"button"}}',
        b'{"expected_version":NaN}',
        b"[]",
        b"null",
        b"\xff",
        b'{"input":{"type":"text","value":"\\ud800"}}',
        b'{"input":{"type":"text","value":"\\u0000"}}',
        b"[" * 2000 + b"0" + b"]" * 2000,
        b" " * (16 * 1024 + 1),
    ],
)
def test_message_rejects_damaged_json(client: TestClient, raw: bytes) -> None:
    response = client.post(
        "/api/v1/chat/messages",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"] == {}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "input",
    [
        {"type": "feedback", "useful": "true"},
        {"type": "feedback", "useful": 1},
        {"type": "button", "option_id": "a", "next": "result"},
        {"type": "button", "option_id": "a", "node_id": "result"},
        {"type": "text", "value": " "},
        {"type": "text", "value": "SYNTHETIC" * 1000},
    ],
)
def test_message_input_is_strict(client: TestClient, input: dict[str, object]) -> None:
    response = client.post(
        "/api/v1/chat/messages",
        json={
            "session_id": str(uuid4()),
            "request_id": str(uuid4()),
            "expected_version": 0,
            "input": input,
        },
    )
    assert response.status_code == 422
    assert "SYNTHETIC" not in response.text
