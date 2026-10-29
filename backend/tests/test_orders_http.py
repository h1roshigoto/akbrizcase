"""Strict transport rejection before touching any database or personal data."""

import json
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.main import app
from app.modules.orders.security import OrderSecurity
from app.schemas.common import OrderHeaders
from app.schemas.orders import OrderCreate


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
def client(monkeypatch: MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.delenv("DB_PASSWORD_FILE", raising=False)
    with TestClient(app) as result:
        yield result


@pytest.mark.parametrize(
    "field,value",
    [
        ("confirmed", False),
        ("confirmed", None),
        ("confirmed", "true"),
        ("consent", {"version": "v", "accepted": False}),
        ("consent", {"version": "v"}),
        ("consent", None),
        ("contact", {}),
        ("contact", {"name": " "}),
        ("contact", {"name": "Synthetic", "email": "invalid"}),
        ("contact", {"name": "Synthetic", "phone": "123"}),
        ("contact", {"name": "Synthetic", "email": "a@example.invalid", "price": "1"}),
        ("comment", None),
        ("comment", ""),
        ("comment", " \t\n"),
        ("comment", "s" * 2001),
        ("kind", "unknown"),
        ("source", "chat"),
        ("chat_session_id", None),
        ("price", "1.00"),
        ("status", "new"),
        ("number", 123),
        ("consent_id", str(uuid4())),
        ("delivery_cost", "0.00"),
        ("snapshot", {}),
        ("tax", "0"),
    ],
)
def test_invalid_input(client: TestClient, field: str, value: object) -> None:
    body = support_body() | {field: value}
    response = client.post("/api/v1/orders", json=body, headers=order_headers())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.headers["cache-control"] == "no-store"
    assert "synthetic@example.invalid" not in response.text


@pytest.mark.parametrize(
    "field", ["confirmed", "consent", "contact", "comment", "items"]
)
def test_missing_fields(client: TestClient, field: str) -> None:
    body = support_body()
    del body[field]
    assert (
        client.post("/api/v1/orders", json=body, headers=order_headers()).status_code
        == 422
    )


@pytest.mark.parametrize("count,qty", [(0, 1), (51, 1), (1, 0), (1, 1000), (1, True)])
def test_invalid_commercial(client: TestClient, count: int, qty: int) -> None:
    body = support_body() | {
        "kind": "commercial",
        "items": [
            {"item_id": str(uuid4()), "qty": qty, "expected_revision": 1}
            for _ in range(count)
        ],
    }
    assert (
        client.post("/api/v1/orders", json=body, headers=order_headers()).status_code
        == 422
    )


@pytest.mark.parametrize("change", ["duplicate", "uuid", "price", "support"])
def test_invalid_selection(client: TestClient, change: str) -> None:
    item = {"item_id": str(uuid4()), "qty": 1, "expected_revision": 1}
    items = [deepcopy(item)]
    if change == "duplicate":
        items.append(item)
    if change == "uuid":
        items[0]["item_id"] = "invalid"
    if change == "price":
        items[0]["price"] = "1.00"
    body = support_body() | {
        "kind": "support" if change == "support" else "commercial",
        "items": items,
    }
    assert (
        client.post("/api/v1/orders", json=body, headers=order_headers()).status_code
        == 422
    )


@pytest.mark.parametrize(
    "name,value",
    [
        ("Idempotency-Key", None),
        ("Idempotency-Key", "invalid"),
        ("X-Request-Token", None),
        ("X-Request-Token", "a" * 63),
        ("X-Request-Token", "A" * 64),
        ("X-Request-Token", "g" * 64),
    ],
)
def test_invalid_headers(client: TestClient, name: str, value: str | None) -> None:
    headers = order_headers()
    if value is None:
        del headers[name]
    else:
        headers[name] = value
    response = client.post("/api/v1/orders", json=support_body(), headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["details"] == {}


@pytest.mark.parametrize(
    "raw",
    [
        b'{"contact":{"name":"private","name":"duplicate"}}',
        b'{"x":NaN}',
        b'"\\ud800"',
        b"\xff",
        b"{",
        b"[]",
        b" " * 65537,
    ],
)
def test_invalid_json(client: TestClient, raw: bytes) -> None:
    response = client.post(
        "/api/v1/orders",
        content=raw,
        headers=order_headers() | {"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "private" not in response.text


def test_stream_limit_without_content_length(client: TestClient) -> None:
    response = client.post(
        "/api/v1/orders",
        content=iter([b" " * 40000, b" " * 40000]),
        headers=order_headers() | {"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_utf16_and_valid_chat_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/orders",
        content=json.dumps(support_body()).encode("utf-16"),
        headers=order_headers() | {"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    response = client.post(
        "/api/v1/orders",
        json=support_body() | {"source": "chat", "chat_session_id": str(uuid4())},
        headers=order_headers(),
    )
    assert response.status_code == 422


def test_missing_database_and_secret(client: TestClient) -> None:
    for path in ("/api/v1/consent-text", "/api/v1/orders"):
        response = (
            client.get(path)
            if path.endswith("text")
            else client.post(path, json=support_body(), headers=order_headers())
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert response.headers["cache-control"] == "no-store"


def test_hashes_use_canonical_bytes_and_protected_key() -> None:
    body = support_body()
    request = OrderCreate.model_validate(body)
    security = OrderSecurity(b"synthetic-test-key" * 3)
    changed_key = OrderSecurity(b"different-test-key" * 3)
    assert security.request_hash(request) != changed_key.request_hash(request)
    assert security.request_hash(request) == security.request_hash(
        OrderCreate.model_validate_json(json.dumps(body, indent=2))
    )
    assert security.request_hash(request) != security.request_hash(
        OrderCreate.model_validate(body | {"comment": "Different description"})
    )
    assert "synthetic-test-key" not in repr(security)
    assert "a" * 64 not in repr(OrderHeaders.model_validate(order_headers()))


def test_secret_fails_closed(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ORDER_HMAC_SECRET_FILE", raising=False)
    assert OrderSecurity.from_environment() is None
    path = tmp_path / "synthetic-secret"
    monkeypatch.setenv("ORDER_HMAC_SECRET_FILE", str(path))
    assert OrderSecurity.from_environment() is None
    path.write_bytes(b"too-short")
    assert OrderSecurity.from_environment() is None
    path.write_bytes(b"synthetic-only-key" * 3)
    assert OrderSecurity.from_environment() is not None


def test_openapi_documents_strict_body_and_headers(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/orders"]["post"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body["additionalProperties"] is False
    assert set(body["required"]) == {
        "kind",
        "items",
        "contact",
        "source",
        "consent",
        "confirmed",
    }
    for parameter in operation["parameters"]:
        assert parameter["required"] is (parameter["name"] != "X-Chat-Token")
    assert {row["name"] for row in operation["parameters"]} == {
        "Idempotency-Key",
        "X-Request-Token",
        "X-Chat-Token",
    }


@pytest.mark.parametrize("tokens", [[], ["a" * 63], ["a" * 64, "b" * 64]])
def test_chat_requires_unambiguous_transport_token(
    client: TestClient, tokens: list[str]
) -> None:
    response = client.post(
        "/api/v1/orders",
        json=support_body() | {"source": "chat", "chat_session_id": str(uuid4())},
        headers=[*order_headers().items(), *[("X-Chat-Token", t) for t in tokens]],
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"] == {}
