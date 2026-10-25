"""Creation evidence only; replay and key-retention policy belong to MVP-010."""

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.contracts.canonical import canonical_order_body
from app.schemas.orders import OrderCreate


@dataclass(frozen=True)
class OrderSecurity:
    secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.secret) < 32:
            raise ValueError("Order security configuration unavailable")

    @classmethod
    def from_environment(cls) -> "OrderSecurity | None":
        try:
            return cls(Path(os.environ["ORDER_HMAC_SECRET_FILE"]).read_bytes().strip())
        except (KeyError, OSError, ValueError):
            return None

    def request_hash(self, order: OrderCreate) -> str:
        return hmac.new(
            self.secret, canonical_order_body(order), hashlib.sha256
        ).hexdigest()
