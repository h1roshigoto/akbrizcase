"""Both clients share the same synthetic fixtures and generated wire types."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.contracts.canonical import canonical_order_body, parse_order_json
from app.contracts.export_types import API_DIR, FIXTURES, render_fixtures, render_types
from app.schemas.admin import OrderStatusChange
from app.schemas.catalog import CatalogItem, CatalogPage, CatalogQuery
from app.schemas.chat import ChatMessageCreate, ChatMessageResult, ChatSessionCreated
from app.schemas.common import ChatHeaders, KnowledgePagination, Pagination
from app.schemas.sources import Applicability, KnowledgePage, SourceRef


def fixtures() -> dict[str, Any]:
    result: dict[str, Any] = json.loads((API_DIR / "fixtures.json").read_text())
    return result


@pytest.mark.parametrize("name", FIXTURES)
def test_shared_frontend_fixtures(name: str) -> None:
    model, many = FIXTURES[name]
    value = fixtures()[name]
    for example in value if many else [value]:
        parsed = model.model_validate_json(json.dumps(example))
        assert parsed.model_dump(mode="json", exclude_unset=True) == example


def test_generated_client_contract_has_no_drift() -> None:
    assert (API_DIR / "types.ts").read_text() == render_types()
    assert (API_DIR / "fixtures.ts").read_text() == render_fixtures()


def test_defaults_and_empty_page() -> None:
    assert Pagination().model_dump() == {"limit": 20, "offset": 0}
    assert KnowledgePagination().limit == 5
    assert CatalogPage(items=[], total=0, limit=20, offset=0).items == []
    for field in ("items", "total", "limit", "offset"):
        data: dict[str, Any] = {"items": [], "total": 0, "limit": 20, "offset": 0}
        del data[field]
        with pytest.raises(ValidationError):
            CatalogPage.model_validate(data)


@pytest.mark.parametrize(
    "data",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": "20"},
        {"limit": 1.0},
        {"offset": -1},
        {"offset": 10001},
        {"offset": 0.5},
        {"offset": False},
        {"q": "x" * 1001},
        {"kind": "other"},
        {"model_id": "not-a-uuid"},
        {"status": "published"},
    ],
)
def test_catalog_query_limits(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CatalogQuery.model_validate(data)


def test_knowledge_limit_and_required_arrays() -> None:
    body = fixtures()["knowledge_page"]
    with pytest.raises(ValidationError):
        KnowledgePage.model_validate(body | {"limit": 6})
    with pytest.raises(ValidationError):
        CatalogPage.model_validate(
            {"items": None, "total": 0, "limit": 20, "offset": 0}
        )
    body = fixtures()["catalog_page"]
    with pytest.raises(ValidationError):
        CatalogPage.model_validate(body | {"total": 1})
    with pytest.raises(ValidationError):
        CatalogPage.model_validate(body | {"limit": 1})


@pytest.mark.parametrize(
    "change",
    [
        {"price": 1200.0},
        {"price_mode": "fixed", "price": None},
        {"price_mode": "on_request", "price": "12.00"},
        {"price": "-1"},
        {"price": "1e2"},
        {"revision": 0},
        {"revision": True},
        {"kind": "service", "requires_shipping": True},
        {"currency": "USD"},
        {"requires_shipping": 1},
        {"image_key": "private-storage-key"},
        {"image_url": "https://example.org/image.png"},
        {"image_url": "//example.org/image.png"},
        {
            "image_url": "/api/v1/catalog/items/"
            "00000000-0000-4000-8000-000000000001/image/../private"
        },
        {
            "image_url": "/api/v1/catalog/items/"
            "00000000-0000-4000-8000-000000000001/image/%2e%2e"
        },
        {
            "image_url": "/api/v1/catalog/items/"
            "00000000-0000-4000-8000-000000000002/image/demo.png"
        },
        {
            "image_url": "/api/v1/catalog/items/"
            "00000000-0000-4000-8000-000000000001/image/demo.png?token=x"
        },
    ],
)
def test_catalog_invariants(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CatalogItem.model_validate(fixtures()["catalog_item"] | change)


def test_money_canonical_output() -> None:
    item = CatalogItem.model_validate(fixtures()["catalog_item"] | {"price": "1200"})
    assert json.loads(item.model_dump_json())["price"] == "1200.00"
    assert (
        item.model_json_schema()["properties"]["price"]["anyOf"][0]["type"] == "string"
    )
    for field in ("price", "image_url", "applicability"):
        value = fixtures()["catalog_item"]
        del value[field]
        with pytest.raises(ValidationError):
            CatalogItem.model_validate(value)


def source() -> dict[str, Any]:
    value: dict[str, Any] = fixtures()["knowledge_page"]["items"][0]["source"]
    return value


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://example.org/file"},
        {"url": "//example.org/file"},
        {"url": "/api/v1/documents/../private"},
        {"document_id": "00000000-0000-4000-8000-000000000099"},
        {"document_version_id": "00000000-0000-4000-8000-000000000099"},
        {"page_from": 0, "page_to": 1},
        {"page_from": 2, "page_to": 1},
        {"page_from": None, "page_to": 1},
        {"page_from": 1, "page_to": None},
        {"page_from": True, "page_to": 1},
        {"file_key": "private-key"},
        {"section_id": "https://example.org"},
    ],
)
def test_source_ref_constraints(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SourceRef.model_validate(source() | change)


@pytest.mark.parametrize("suffix", ["?token=x", "#page=1", "/..", "%2f", "/other"])
def test_document_link_has_no_suffix(suffix: str) -> None:
    value = source()
    with pytest.raises(ValidationError):
        SourceRef.model_validate(value | {"url": value["url"] + suffix})


def test_source_pages_and_confirmed_without_public_evidence() -> None:
    assert SourceRef.model_validate(source() | {"page_from": 1, "page_to": 2})
    value = {"model_id": "00000000-0000-4000-8000-000000000003", "sources": []}
    assert Applicability.model_validate(value | {"status": "confirmed"}).sources == []
    assert Applicability.model_validate(value | {"status": "unknown"}).sources == []
    with pytest.raises(ValidationError):
        Applicability.model_validate(
            value | {"status": "unknown", "sources": [source()]}
        )
    with pytest.raises(ValidationError):
        Applicability.model_validate(value | {"status": "incompatible"})


@pytest.mark.parametrize(
    "value",
    [
        {"type": "text", "value": ""},
        {"type": "text", "value": " \n"},
        {"type": "text", "value": "x" * 1001},
        {"type": "text", "value": 123},
        {"type": "select_mode", "mode": "repair"},
        {"type": "start_diagnosis", "model_id": "bad", "tree_key": "demo"},
        {
            "type": "start_diagnosis",
            "model_id": "00000000-0000-4000-8000-000000000003",
            "tree_key": "../tree",
        },
        {"type": "button", "option_id": "demo", "next": "result"},
        {"type": "button", "option_id": "https://example.org"},
        {"type": "feedback", "useful": "true"},
        {"type": "feedback", "useful": 1},
        {"type": "run_code", "value": "anything"},
        {},
        None,
    ],
)
def test_invalid_chat_inputs(value: object) -> None:
    body = fixtures()["chat_messages"][0]
    with pytest.raises(ValidationError):
        ChatMessageCreate.model_validate(body | {"input": value})


@pytest.mark.parametrize("value", [-1, True, 1.5, "0"])
def test_chat_versions_are_strict(value: object) -> None:
    body = fixtures()["chat_messages"][0]
    with pytest.raises(ValidationError):
        ChatMessageCreate.model_validate(body | {"expected_version": value})


@pytest.mark.parametrize(
    "action",
    [
        {"type": "open_url", "payload": {"url": "https://example.org"}},
        {"type": "open_item", "payload": {"item_id": "https://example.org"}},
        {
            "type": "open_item",
            "payload": {
                "item_id": "00000000-0000-4000-8000-000000000001",
                "url": "https://example.org",
            },
        },
        {"type": "open_document", "payload": {"url": "https://example.org"}},
        {"type": "contact_specialist", "payload": {"kind": "commercial"}},
        {
            "type": "open_request_form",
            "payload": {
                "kind": "support",
                "items": [
                    {"item_id": "00000000-0000-4000-8000-000000000001", "qty": 1}
                ],
            },
        },
        {"type": "open_request_form", "payload": {"kind": "commercial", "items": []}},
    ],
)
def test_invalid_actions(action: dict[str, object]) -> None:
    body = fixtures()["chat_result"]
    body["reply"]["actions"] = [action]
    with pytest.raises(ValidationError):
        ChatMessageResult.model_validate(body)


def test_chat_reply_has_arrays_and_no_confidence() -> None:
    body = fixtures()["chat_result"]
    for field in ("buttons", "sources", "items", "actions"):
        modified = deepcopy(body)
        del modified["reply"][field]
        with pytest.raises(ValidationError):
            ChatMessageResult.model_validate(modified)
    body["reply"]["confidence"] = 0.9
    with pytest.raises(ValidationError):
        ChatMessageResult.model_validate(body)


def test_session_response_has_utc_and_secret_separate_from_url() -> None:
    value = {
        "session_id": "00000000-0000-4000-8000-000000000010",
        "session_token": "0" * 64,  # Synthetic shape, never a real token.
        "state_version": 0,
        "expires_at": "2026-10-25T00:00:00Z",
        "reply": fixtures()["chat_result"]["reply"],
    }
    assert (
        json.loads(
            ChatSessionCreated.model_validate(value).model_dump_json(exclude_unset=True)
        )
        == value
    )
    for change in (
        {"expires_at": "2026-10-25T00:00:00"},
        {"expires_at": "2026-10-25T03:00:00+03:00"},
        {"session_token": "https://example.org/token"},
        {"state_version": 1},
        {"state_version": False},
        {"state_version": 0.0},
    ):
        with pytest.raises(ValidationError):
            ChatSessionCreated.model_validate(value | change)
    assert ChatHeaders.model_validate({"X-Chat-Token": "0" * 64})


@pytest.mark.parametrize(
    "body",
    [
        {"status": "arbitrary", "expected_version": 1},
        {"status": "new", "expected_version": 1},
        {"status": "closed", "expected_version": 1},
        {"status": "cancelled", "expected_version": 1, "reason": " "},
        {"status": "in_progress", "expected_version": True},
        {"status": "in_progress", "expected_version": 0},
        {"status": "in_progress", "expected_version": 1, "actor_id": "client"},
    ],
)
def test_admin_status_validation(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OrderStatusChange.model_validate(body)


def test_raw_json_canonicalization_and_ambiguous_keys() -> None:
    body = fixtures()["commercial_order"]
    first = parse_order_json(json.dumps(body, ensure_ascii=True, indent=2))
    second = parse_order_json(
        json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    )
    assert canonical_order_body(first) == canonical_order_body(second)
    for invalid in (
        '{"kind":"support","kind":"commercial"}',
        '{"contact":{"name":"first","name":"second"}}',
        '{"qty":NaN}',
        '{"qty":Infinity}',
        '{"comment":"\\ud800"}',
    ):
        with pytest.raises(ValueError):
            parse_order_json(invalid)


def test_fixture_registry_covers_all_examples() -> None:
    assert set(fixtures()) == set(FIXTURES)
    assert API_DIR == Path(__file__).resolve().parents[3] / "frontend/src/api"
    assert all(issubclass(model, BaseModel) for model, _ in FIXTURES.values())
