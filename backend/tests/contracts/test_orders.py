"""Commercial contracts are executable without API endpoints or a database."""

import json
from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.contracts.canonical import canonical_order_body
from app.schemas.common import OrderHeaders
from app.schemas.orders import OrderCreate, OrderReceipt

ITEM_ID = "00000000-0000-4000-8000-000000000001"


def commercial() -> dict[str, Any]:
    return {
        "kind": "commercial",
        "items": [{"item_id": ITEM_ID, "qty": 2, "expected_revision": 3}],
        "contact": {"name": "Демо-покупатель", "email": "buyer@example.org"},
        "organization_name": "Демо-организация",
        "city": "Демо-город",
        "comment": "Нужно коммерческое предложение",
        "source": "site",
        "consent": {"version": "DEMO-v1", "accepted": True},
        "confirmed": True,
    }


def test_architecture_commercial_example() -> None:
    body = commercial()
    order = OrderCreate.model_validate_json(json.dumps(body))
    assert order.model_dump(mode="json", exclude_unset=True) == body


@pytest.mark.parametrize("field", ["price", "status", "unknown", "consent_id"])
def test_unknown_order_fields(field: str) -> None:
    body = commercial()
    body[field] = "arbitrary"
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(body)


@pytest.mark.parametrize("location", ["items", "contact", "consent"])
def test_nested_unknown_fields(location: str) -> None:
    body = commercial()
    nested = body[location][0] if location == "items" else body[location]
    nested["price"] = "1.00"
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(body)


@pytest.mark.parametrize("value", [0, -1, 1000, 2.5, "2", True, None])
def test_strict_quantity(value: object) -> None:
    body = commercial()
    body["items"][0]["qty"] = value
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(body)


@pytest.mark.parametrize("value", [0, -1, 1.5, "3", True])
def test_strict_revision(value: object) -> None:
    body = commercial()
    body["items"][0]["expected_revision"] = value
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(body)


@pytest.mark.parametrize("value", [False, 1, "true", None])
@pytest.mark.parametrize("field", ["confirmed", "accepted"])
def test_explicit_confirmation(field: str, value: object) -> None:
    body = commercial()
    if field == "accepted":
        body["consent"][field] = value
    else:
        body[field] = value
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(body)


@pytest.mark.parametrize("comment", [None, "", "   \n\t", "x" * 2001])
@pytest.mark.parametrize("source", ["site", "chat"])
def test_support_requires_comment(comment: str | None, source: str) -> None:
    body = commercial() | {"kind": "support", "items": [], "source": source}
    body.pop("comment")
    if comment is not None:
        body["comment"] = comment
    if source == "chat":
        body["chat_session_id"] = ITEM_ID
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(body)


def test_support_accepts_independent_description_and_preserves_text() -> None:
    body = commercial() | {
        "kind": "support",
        "items": [],
        "comment": "  Демонстрационное описание проблемы  ",
    }
    assert OrderCreate.model_validate(body).comment == body["comment"]


@pytest.mark.parametrize("items", [[], [commercial()["items"][0]] * 2])
def test_commercial_requires_unique_nonempty_items(items: list[object]) -> None:
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(commercial() | {"items": items})


def test_item_limit_and_support_items() -> None:
    body = commercial()
    body["items"] *= 51
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(body)
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(commercial() | {"kind": "support"})


@pytest.mark.parametrize(
    "change",
    [
        {"source": "chat"},
        {"source": "site", "chat_session_id": ITEM_ID},
        {"source": "site", "chat_session_id": None},
        {"source": "chat", "chat_session_id": "not-a-uuid"},
        {"source": "other"},
    ],
)
def test_source_shape(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(commercial() | change)


@pytest.mark.parametrize(
    "contact",
    [
        {"name": "Демо"},
        {"name": " ", "email": "buyer@example.org"},
        {"name": "x" * 201, "email": "buyer@example.org"},
        {"name": "Демо", "email": "invalid"},
        {"name": "Демо", "email": "a..b@example.org"},
        {"name": "Демо", "email": "buyer@example..org"},
        {"name": "Демо", "email": "buyer@-example.org"},
        {"name": "Демо", "email": "x" * 244 + "@example.org"},
        {"name": "Демо", "phone": "not a phone"},
        {"name": "Демо", "phone": 1234567},
        {"name": "Демо", "phone": "1" * 33},
    ],
)
def test_contact_validation(contact: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(commercial() | {"contact": contact})


def receipt() -> dict[str, Any]:
    return {
        "number": 1,
        "status": "new",
        "known_subtotal": "2400.00",
        "items_total": None,
        "delivery_status": "pending_quote",
        "delivery_cost": None,
        "total_amount": None,
        "currency": "RUB",
    }


def test_receipt_decimal_null_and_safe_fields() -> None:
    dto = OrderReceipt.model_validate(receipt())
    assert dto.known_subtotal == Decimal("2400.00")
    assert json.loads(dto.model_dump_json()) == receipt()
    with pytest.raises(ValidationError):
        OrderReceipt.model_validate(receipt() | {"contact": commercial()["contact"]})
    with pytest.raises(ValidationError):
        OrderReceipt.model_validate(receipt() | {"status": "in_progress"})


@pytest.mark.parametrize(
    "money", [2400, 2400.0, True, "NaN", "Infinity", "-1", "1e3", "1.001"]
)
def test_money_is_never_a_json_number(money: object) -> None:
    with pytest.raises(ValidationError):
        OrderReceipt.model_validate(receipt() | {"known_subtotal": money})


def test_unknown_total_is_not_zero() -> None:
    with pytest.raises(ValidationError):
        OrderReceipt.model_validate(receipt() | {"total_amount": "0.00"})


def test_required_nullable_fields_cannot_be_omitted() -> None:
    for field in ("items_total", "delivery_cost", "total_amount"):
        body = receipt()
        del body[field]
        with pytest.raises(ValidationError):
            OrderReceipt.model_validate(body)


def test_idempotency_headers() -> None:
    synthetic = {"Idempotency-Key": ITEM_ID, "X-Request-Token": "0" * 64}
    assert OrderHeaders.model_validate(synthetic).idempotency_key
    for name in synthetic:
        missing = dict(synthetic)
        del missing[name]
        with pytest.raises(ValidationError):
            OrderHeaders.model_validate(missing)
    with pytest.raises(ValidationError):
        OrderHeaders.model_validate(
            synthetic | {"X-Request-Token": "https://example.org"}
        )


def canonical(body: dict[str, Any]) -> bytes:
    return canonical_order_body(OrderCreate.model_validate(body))


def test_canonicalization_has_stable_utf8_json_bytes() -> None:
    body = commercial()
    expected = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert canonical(body) == expected
    assert canonical(body) == canonical(dict(reversed(list(body.items()))))
    reordered = deepcopy(body)
    for name in ("contact", "consent"):
        reordered[name] = dict(reversed(list(body[name].items())))
    reordered["items"][0] = dict(reversed(list(body["items"][0].items())))
    assert canonical(body) == canonical(reordered)
    assert canonical(body).decode("utf-8").startswith('{"city":"Демо-город"')


def test_canonicalization_preserves_array_order_and_exact_text() -> None:
    body = commercial()
    body["items"].append(
        {
            "item_id": "00000000-0000-4000-8000-000000000002",
            "qty": 1,
            "expected_revision": 1,
        }
    )
    reordered = deepcopy(body)
    reordered["items"].reverse()
    assert canonical(body) != canonical(reordered)
    assert canonical(body) != canonical(body | {"comment": body["comment"] + " "})
    assert canonical(body | {"comment": "é"}) != canonical(
        body | {"comment": "e\u0301"}
    )


def test_canonicalization_preserves_omission_vs_null() -> None:
    body = commercial()
    del body["comment"]
    assert canonical(body) != canonical(body | {"comment": None})
    body["contact"].pop("email")
    body["contact"]["phone"] = "+0 000 000 0000"
    omitted = canonical(body)
    body["contact"]["email"] = None
    assert canonical(body) != omitted


def test_canonicalization_golden_bytes() -> None:
    order = OrderCreate.model_validate(
        {
            "kind": "support",
            "items": [],
            "contact": {"name": "Demo", "email": "demo@example.org"},
            "comment": "Demo",
            "source": "site",
            "consent": {"version": "demo", "accepted": True},
            "confirmed": True,
        }
    )
    expected = (
        b'{"comment":"Demo","confirmed":true,"consent":{"accepted":true,'
        b'"version":"demo"},"contact":{"email":"demo@example.org","name":"Demo"},'
        b'"items":[],"kind":"support","source":"site"}'
    )
    assert canonical_order_body(order) == expected
