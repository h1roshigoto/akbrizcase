"""Protected, bounded HMAC keyring; legacy single-key files remain valid."""

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.contracts.canonical import canonical_order_body
from app.schemas.orders import OrderCreate


@dataclass(frozen=True)
class OrderSecurity:
    secret: bytes = field(repr=False)
    previous: tuple[bytes, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        keys = (self.secret, *self.previous)
        if (
            len(keys) > 4
            or any(not 32 <= len(key) <= 512 for key in keys)
            or len(set(keys)) != len(keys)
        ):
            raise ValueError("Order security configuration unavailable")

    @classmethod
    def from_environment(
        cls, variable: str = "ORDER_HMAC_SECRET_FILE"
    ) -> "OrderSecurity | None":
        try:
            with Path(os.environ[variable]).open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError
            raw = raw.strip()
            if not raw.startswith(b"{"):
                return cls(raw)

            def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for name, value in pairs:
                    if name in result:
                        raise ValueError
                    result[name] = value
                return result

            ring = json.loads(raw, object_pairs_hook=unique_object)
            if (
                not isinstance(ring, dict)
                or set(ring) != {"active", "previous"}
                or not isinstance(ring["previous"], list)
                or len(ring["previous"]) > 3
            ):
                raise ValueError
            keys = [ring["active"], *ring["previous"]]
            if any(
                not isinstance(key, str) or re.fullmatch(r"[!-~]{32,512}", key) is None
                for key in keys
            ):
                raise ValueError
            # Preserve textual bytes exactly, including legacy URL-safe keys.
            return cls(
                keys[0].encode("ascii"), tuple(k.encode("ascii") for k in keys[1:])
            )
        except (KeyError, OSError, ValueError, TypeError):
            return None

    def request_hash(self, order: OrderCreate) -> str:
        return hmac.new(
            self.secret, canonical_order_body(order), hashlib.sha256
        ).hexdigest()

    def matches_request(self, order: OrderCreate, saved: str) -> bool:
        """Call only after token authentication. Never retain canonical PII."""
        canonical = canonical_order_body(order)
        matches = False
        for key in (self.secret, *self.previous):
            candidate = hmac.new(key, canonical, hashlib.sha256).hexdigest()
            matches |= hmac.compare_digest(candidate, saved)
        return matches
