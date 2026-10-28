"""Strict original identity is distinct from minimized, still personal storage."""

import json
from uuid import uuid4

import pytest

from app.modules.chat.message_request import (
    minimized_input,
    parse_message_json,
    request_hash,
)


def request(value: str = "  Синтетический запрос\n") -> bytes:
    return json.dumps(
        {
            "session_id": str(uuid4()),
            "request_id": str(uuid4()),
            "expected_version": 0,
            "input": {"type": "text", "value": value},
        },
        ensure_ascii=False,
    ).encode()


def test_canonical_identity_preserves_text_and_normalizes_dto_fields() -> None:
    original = parse_message_json(request())
    raw = original.model_dump(mode="json")
    raw["session_id"] = raw["session_id"].upper()
    raw["request_id"] = raw["request_id"].upper()
    normalized = parse_message_json(json.dumps(raw, sort_keys=True).encode())
    assert request_hash(original) == request_hash(normalized)
    assert minimized_input(original)["value"] == "  Синтетический запрос\n"
    raw["input"]["value"] = raw["input"]["value"].strip()
    assert request_hash(original) != request_hash(
        parse_message_json(json.dumps(raw).encode())
    )
    raw["input"] = {
        "type": "start_diagnosis",
        "model_id": str(uuid4()),
        "tree_key": "SYNTHETIC",
    }
    first = parse_message_json(json.dumps(raw).encode())
    raw["input"]["model_id"] = raw["input"]["model_id"].upper()
    assert request_hash(first) == request_hash(
        parse_message_json(json.dumps(raw).encode())
    )


@pytest.mark.parametrize(
    "value",
    [
        "synthetic@example.invalid",
        "+7 (999) 123-45-67",
        "+1-202-555-0199",
        "8 999 123 45 67",
        "2025550199",
    ],
)
def test_minimize_typical_contacts(value: str) -> None:
    body = parse_message_json(request(value))
    assert minimized_input(body)["value"] in {"[email]", "[phone]"}
    assert body.input.type == "text" and body.input.value == value


@pytest.mark.parametrize(
    "raw",
    [
        b'{"session_id":"one","session_id":"two"}',
        b'{"input":{"type":"text","type":"button"}}',
        b'{"input":{"value":NaN}}',
        b'{"input":{"value":Infinity}}',
        b"{} trailing",
        b"[]",
        b"null",
        b"\xff",
    ],
)
def test_ambiguous_and_damaged_json_is_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_message_json(raw)


@pytest.mark.parametrize("value", ["\x00", "\ud800", "\udfff"])
def test_non_jsonb_unicode_is_rejected(value: str) -> None:
    raw = json.loads(request())
    raw["input"]["value"] = value
    with pytest.raises(ValueError):
        parse_message_json(json.dumps(raw).encode())


def test_literal_unicode_escape_is_user_content() -> None:
    body = parse_message_json(request(r"literal \u0000"))
    assert minimized_input(body)["value"] == r"literal \u0000"
