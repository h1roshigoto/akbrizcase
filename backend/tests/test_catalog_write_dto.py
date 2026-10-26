"""Strict commercial commands, money bounds and bounded display-only JSON."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.modules.catalog.write_dto import ItemCreate, ItemEdit, ModelFields

INVALID_ATTRIBUTES: list[Any] = [
    [],
    "text",
    None,
    {"bad": float("nan")},
    {"bad": float("inf")},
    {"bad": "\x00"},
    {"\x00": 1},
    {"a": "x" * 32769},
    {str(i): i for i in range(201)},
    {"a": list(range(2000))},
]


def fields(**changes: Any) -> dict[str, Any]:
    return {
        "sku": "SYNTHETIC-016",
        "kind": "product",
        "name": "Synthetic item",
        "description": "",
        "attributes": {},
        "unit": "unit",
        "price_mode": "fixed",
        "price": "123.45",
        "currency": "RUB",
        "tax_note": "Synthetic tax note",
        "requires_shipping": False,
        **changes,
    }


@pytest.mark.parametrize("kind", ["product", "service"])
@pytest.mark.parametrize("price", ["0", "0.00", "123.45", "9999999999.99"])
def test_exact_fixed_money(kind: str, price: str) -> None:
    item = ItemCreate.model_validate(fields(kind=kind, price=price))
    assert item.model_dump(mode="json")["price"] == f"{item.price:.2f}"


@pytest.mark.parametrize("kind", ["product", "service"])
def test_unknown_price_remains_null(kind: str) -> None:
    assert (
        ItemCreate.model_validate(
            fields(kind=kind, price_mode="on_request", price=None)
        ).price
        is None
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"price": None},
        {"price_mode": "on_request", "price": "0"},
        {"price_mode": "on_request", "price": "123.45"},
        *(
            {"price": value}
            for value in [
                "-1",
                "NaN",
                "Infinity",
                "-Infinity",
                "abc",
                "1.001",
                "1.000",
                "10000000000.00",
                1,
                0.0,
                True,
            ]
        ),
        {"kind": "service", "requires_shipping": True},
        {"currency": "USD"},
        {"requires_shipping": "false"},
        {"sku": "invalid SKU"},
        {"sku": ""},
        {"kind": "unknown"},
        {"name": " "},
        {"unit": ""},
        {"tax_note": " "},
        {"description": "x" * 10001},
        {"name": "x" * 201},
        {"name": "\x00"},
        {"description": "\ud800"},
        *(
            {field: "injected"}
            for field in [
                "id",
                "revision",
                "created_at",
                "updated_at",
                "actor_id",
                "status",
                "image_key",
            ]
        ),
        *({"attributes": value} for value in INVALID_ATTRIBUTES),
    ],
)
def test_invalid_commands(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ItemCreate.model_validate(fields(**changes))


def test_nested_json_limits_and_literal_html() -> None:
    attack = '<img src=x onerror="window.catalogInjected=true"> & "'
    attrs: dict[str, Any] = {"data": [attack, {"nested": True}, None]}
    assert ItemCreate.model_validate(fields(attributes=attrs)).attributes == attrs
    for _ in range(9):
        attrs = {"nested": attrs}
    with pytest.raises(ValidationError):
        ItemCreate.model_validate(fields(attributes=attrs))


@pytest.mark.parametrize("revision", [0, -1, True, 1.0, "1", 2147483648])
def test_strict_revision(revision: Any) -> None:
    values = fields()
    del values["sku"], values["kind"]
    with pytest.raises(ValidationError):
        ItemEdit.model_validate({**values, "expected_revision": revision})


@pytest.mark.parametrize(
    "changes",
    [
        {"code": "invalid code"},
        {"name": " "},
        {"manual_document_id": "future"},
        {"status": "published"},
        {"description": "x" * 10001},
    ],
)
def test_model_allowlist(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelFields.model_validate(
            {
                "code": "MODEL-016",
                "name": "Synthetic model",
                "description": "",
                **changes,
            }
        )
