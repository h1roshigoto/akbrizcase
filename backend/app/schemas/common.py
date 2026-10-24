"""Shared strict wire types for the v2.1 API contract."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must contain non-whitespace text")
    return value


Text200 = Annotated[
    str, StringConstraints(min_length=1, max_length=200), AfterValidator(nonblank)
]
Code = Annotated[
    str, StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
]
Comment = Annotated[
    str, StringConstraints(min_length=1, max_length=2000), AfterValidator(nonblank)
]
Query = Annotated[str, StringConstraints(max_length=1000)]
PositiveInt = Annotated[int, Field(ge=1)]
Version = Annotated[int, Field(ge=0)]
Quantity = Annotated[int, Field(ge=1, le=999)]
Currency = Literal["RUB"]
OrderKind = Literal["commercial", "support"]
OrderStatus = Literal["new", "in_progress", "closed", "cancelled"]
CatalogKind = Literal["product", "service"]
PriceMode = Literal["fixed", "on_request"]


def parse_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValueError("UUID must be a string")
    parsed = UUID(value)
    if str(parsed) != value.lower():
        raise ValueError("UUID must use the hyphenated representation")
    return parsed


UUIDValue = Annotated[UUID, BeforeValidator(parse_uuid)]
# Wire encoding: 32 random bytes represented as 64 lowercase hexadecimal digits.
SecretToken = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Checksum = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def true_only(value: bool) -> bool:
    if value is not True:
        raise ValueError("explicit true is required")
    return value


Confirmed = Annotated[
    bool,
    AfterValidator(true_only),
    WithJsonSchema({"type": "boolean", "const": True}),
]

MONEY_PATTERN = r"^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$"


def parse_money(value: object) -> Decimal:
    if isinstance(value, Decimal):
        text = format(value, "f")
    elif isinstance(value, str):
        text = value
    else:
        raise ValueError("money must be a decimal string, never a JSON number")
    if re.fullmatch(MONEY_PATTERN, text) is None:
        raise ValueError("money must be nonnegative with at most two decimal places")
    return Decimal(text)


Money = Annotated[
    Decimal,
    BeforeValidator(parse_money),
    PlainSerializer(lambda value: format(value, ".2f"), return_type=str),
    WithJsonSchema({"type": "string", "pattern": MONEY_PATTERN}),
]


def parse_utc(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("an ISO 8601 UTC timestamp is required")
    return value.astimezone(UTC)


UTCDateTime = Annotated[datetime, BeforeValidator(parse_utc)]


class Pagination(DTO):
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    offset: Annotated[int, Field(ge=0, le=10000)] = 0


class KnowledgePagination(Pagination):
    limit: Annotated[int, Field(ge=1, le=5)] = 5


class Page[T](DTO):
    items: list[T]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=100)]
    offset: Annotated[int, Field(ge=0, le=10000)]

    @model_validator(mode="after")
    def page_bounds(self) -> "Page[T]":
        if len(self.items) > self.limit or len(self.items) > self.total:
            raise ValueError("page length exceeds limit or total")
        return self


class ErrorDetail(DTO):
    # Named future errors remain possible; arbitrary free text is not an error code.
    code: Annotated[
        str, StringConstraints(max_length=100, pattern=r"^[A-Z][A-Z0-9_]*$")
    ]
    message: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    details: dict[str, JsonValue]


class ErrorEnvelope(DTO):
    error: ErrorDetail
    request_id: UUIDValue


class OrderHeaders(DTO):
    idempotency_key: UUIDValue = Field(alias="Idempotency-Key")
    request_token: SecretToken = Field(alias="X-Request-Token", repr=False)


class ChatHeaders(DTO):
    chat_token: SecretToken = Field(alias="X-Chat-Token", repr=False)
