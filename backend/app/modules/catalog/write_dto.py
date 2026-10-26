"""Internal staff form commands; public catalog contracts remain unchanged."""

import json
import math
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.schemas.common import (
    DTO,
    CatalogKind,
    Code,
    Currency,
    Money,
    PositiveInt,
    PriceMode,
    Text200,
    UTCDateTime,
)


def postgres_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("NUL is not supported")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid Unicode") from None
    return value


class TextCommand(DTO):
    @field_validator("*", mode="after")
    @classmethod
    def valid_text(cls, value: object) -> object:
        if isinstance(value, str):
            postgres_text(value)
        return value


class ModelFields(TextCommand):
    code: Code
    name: Text200
    description: str = Field(max_length=10000)


class ModelEdit(ModelFields):
    expected_updated_at: UTCDateTime


class ModelState(DTO):
    expected_updated_at: UTCDateTime


class ItemFields(TextCommand):
    name: Text200
    description: str = Field(max_length=10000)
    attributes: Annotated[dict[str, JsonValue], Field(max_length=200)]
    unit: Text200
    price_mode: PriceMode
    price: Annotated[Money, Field(le=Decimal("9999999999.99"))] | None
    currency: Currency
    tax_note: Text200
    requires_shipping: bool

    @field_validator("attributes")
    @classmethod
    def bounded_attributes(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        nodes = 0

        def visit(node: JsonValue, depth: int) -> None:
            nonlocal nodes
            nodes += 1
            if depth > 8 or nodes > 2000:
                raise ValueError("Attributes are too complex")
            if isinstance(node, str):
                postgres_text(node)
            elif isinstance(node, float) and not math.isfinite(node):
                raise ValueError("Attributes require finite numbers")
            elif isinstance(node, dict):
                for key, child in node.items():
                    postgres_text(key)
                    visit(child, depth + 1)
            elif isinstance(node, list):
                for child in node:
                    visit(child, depth + 1)

        visit(value, 0)
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 32768:
            raise ValueError("Attributes exceed 32 KiB")
        return value

    @model_validator(mode="after")
    def price_matrix(self) -> "ItemFields":
        if (self.price_mode == "fixed") != (self.price is not None):
            raise ValueError("fixed requires price; on_request requires null")
        return self


class ItemCreate(ItemFields):
    sku: Annotated[
        str,
        StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.-]+$"),
    ]
    kind: CatalogKind

    @model_validator(mode="after")
    def shipping_matrix(self) -> "ItemCreate":
        if self.kind == "service" and self.requires_shipping:
            raise ValueError("Services cannot require shipping")
        return self


class ItemEdit(ItemFields):
    expected_revision: Annotated[PositiveInt, Field(le=2147483647)]


class ItemState(DTO):
    expected_revision: Annotated[PositiveInt, Field(le=2147483647)]


StateAction = Literal["publish", "archive"]
