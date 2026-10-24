"""POST /orders and safe receipt DTOs; no persistence or submission service."""

import re
from decimal import Decimal
from email.errors import HeaderParseError
from email.headerregistry import Address
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from app.schemas.common import (
    DTO,
    Confirmed,
    Currency,
    Money,
    OrderKind,
    PositiveInt,
    Quantity,
    Text200,
    UUIDValue,
)
from app.schemas.consent import ConsentAcceptance


class Contact(DTO):
    name: Text200
    email: (
        Annotated[
            str,
            StringConstraints(
                min_length=3,
                max_length=254,
                pattern=r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$",
            ),
        ]
        | None
    ) = None
    phone: (
        Annotated[
            str,
            StringConstraints(
                min_length=7, max_length=32, pattern=r"^\+?[0-9][0-9 ()-]*$"
            ),
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def contact_channel(self) -> "Contact":
        if self.email is None and self.phone is None:
            raise ValueError("email or phone is required")
        if self.email is not None:
            try:
                Address(addr_spec=self.email)
            except (HeaderParseError, ValueError) as exc:
                raise ValueError("email must be a valid address") from exc
            local, domain = self.email.rsplit("@", 1)
            labels = domain.encode("idna").decode("ascii").split(".")
            if len(local) > 64 or any(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                is None
                for label in labels
            ):
                raise ValueError("email has an invalid local part or domain")
        if self.phone is not None:
            digits = sum(char.isdigit() for char in self.phone)
            if not 7 <= digits <= 15:
                raise ValueError("phone must contain 7 to 15 digits")
        return self


class OrderItemInput(DTO):
    item_id: UUIDValue
    qty: Quantity
    expected_revision: PositiveInt


class OrderCreate(DTO):
    kind: OrderKind
    items: Annotated[list[OrderItemInput], Field(max_length=50)]
    contact: Contact
    organization_name: Text200 | None = None
    city: Text200 | None = None
    comment: Annotated[str, StringConstraints(max_length=2000)] | None = None
    source: Literal["site", "chat"]
    # Omission is allowed for site; explicit null is not part of this input contract.
    chat_session_id: UUIDValue | None = None
    consent: ConsentAcceptance
    confirmed: Confirmed

    @model_validator(mode="after")
    def order_shape(self) -> "OrderCreate":
        if self.kind == "commercial" and not self.items:
            raise ValueError("commercial requires 1 to 50 items")
        if self.kind == "support" and (
            self.items or self.comment is None or not self.comment.strip()
        ):
            raise ValueError("support requires empty items and a meaningful comment")
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("item_id values must be unique")
        if self.source == "site" and "chat_session_id" in self.model_fields_set:
            raise ValueError("site must omit chat_session_id")
        if self.source == "chat" and self.chat_session_id is None:
            raise ValueError("chat requires chat_session_id and X-Chat-Token")
        return self


class OrderReceipt(DTO):
    number: PositiveInt
    status: Literal["new"]
    known_subtotal: Money
    items_total: Money | None
    delivery_status: Literal["not_required", "pending_quote"]
    delivery_cost: Money | None
    total_amount: Money | None
    currency: Currency

    @model_validator(mode="after")
    def receipt_totals(self) -> "OrderReceipt":
        if self.delivery_status == "pending_quote" and self.delivery_cost is not None:
            raise ValueError("pending delivery must have unknown cost")
        if self.delivery_status == "not_required" and self.delivery_cost != Decimal(0):
            raise ValueError("unneeded delivery must cost zero")
        if self.items_total is not None and self.items_total != self.known_subtotal:
            raise ValueError("complete items total must equal known subtotal")
        expected = (
            self.items_total + self.delivery_cost
            if self.items_total is not None and self.delivery_cost is not None
            else None
        )
        if self.total_amount != expected:
            raise ValueError("total must be null unless all costs are known")
        return self
