"""Canonical POST /orders bytes for a future HMAC (see docs/API_CONTRACTS.md)."""

import json
from typing import Any

from app.schemas.orders import OrderCreate


def parse_order_json(raw: str | bytes) -> OrderCreate:
    """Reject ambiguous duplicate object keys before validating the request body."""

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-JSON numeric constant: {value}")

    body = json.loads(
        raw, object_pairs_hook=unique_object, parse_constant=reject_constant
    )
    # Escaped lone surrogates are accepted by json.loads but are not valid UTF-8.
    json.dumps(body, ensure_ascii=False).encode("utf-8")
    return OrderCreate.model_validate(body)


def canonical_order_body(order: OrderCreate) -> bytes:
    """Preserve input presence, text and array order; sort object keys recursively."""
    return json.dumps(
        order.model_dump(mode="json", exclude_unset=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
