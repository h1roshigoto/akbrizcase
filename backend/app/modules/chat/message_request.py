"""Unambiguous original request identity, separate from minimized storage."""

import hashlib
import json
import re
from typing import Any

from pydantic import JsonValue

from app.schemas.chat import ChatMessageCreate


def parse_message_json(raw: bytes) -> ChatMessageCreate:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("AMBIGUOUS_CHAT_REQUEST")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError("INVALID_CHAT_REQUEST")

    body = json.loads(
        raw, object_pairs_hook=unique_object, parse_constant=reject_constant
    )
    # PostgreSQL JSONB cannot represent NUL or lone UTF-16 surrogates. Reject
    # them before hashing/routing, including within extra or nested fields.
    json.dumps(body, ensure_ascii=False).encode("utf-8")

    def reject_nul(value: object) -> None:
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("INVALID_CHAT_REQUEST")
        if isinstance(value, dict):
            for key, item in value.items():
                reject_nul(key)
                reject_nul(item)
        elif isinstance(value, list):
            for item in value:
                reject_nul(item)

    reject_nul(body)
    return ChatMessageCreate.model_validate(body)


def request_hash(body: ChatMessageCreate) -> str:
    # Scope is (session_id, request_id). Authorization and transport request IDs
    # are never part of the preimage. Preserve user text and array order;
    # normalize DTO UUIDs and recursively sort object keys only.
    canonical = json.dumps(
        {
            "expected_version": body.expected_version,
            "input": body.input.model_dump(mode="json"),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


_EMAIL = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ()\u00a0.\-]{5,}\d(?!\w)")


def minimized_input(body: ChatMessageCreate) -> dict[str, JsonValue]:
    value: dict[str, JsonValue] = body.input.model_dump(mode="json")
    if body.input.type == "text":
        text = _EMAIL.sub("[email]", body.input.value)
        value["value"] = _PHONE.sub(
            lambda match: (
                "[phone]"
                if 7 <= sum(c.isdecimal() for c in match[0]) <= 15
                else match[0]
            ),
            text,
        )
    return value
