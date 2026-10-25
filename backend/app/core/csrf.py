"""Timed, purpose-separated CSRF tokens bound to a host-only browser cookie."""

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from itsdangerous import BadData, URLSafeTimedSerializer

from app.core.staff_sessions import token_hash

SESSION_COOKIE = "__Host-staff_session"
LOGIN_COOKIE = "__Host-staff_login"
LOGIN_SECONDS = 600


def https_origin(value: str) -> bool:
    try:
        url = urlsplit(value)
        return (
            url.scheme == "https"
            and bool(url.hostname)
            and url.username is None
            and url.password is None
            and url.path == url.query == url.fragment == ""
            and value == f"https://{url.netloc}"
            and (url.port is None or 1 <= url.port <= 65535)
            and not any(c.isspace() for c in value)
        )
    except ValueError:
        return False


@dataclass(frozen=True)
class StaffSecurity:
    trusted_origins: frozenset[str]
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            len(self.secret) < 32
            or not self.trusted_origins
            or not all(https_origin(origin) for origin in self.trusted_origins)
        ):
            raise ValueError("Staff security configuration unavailable")

    @classmethod
    def from_environment(cls) -> "StaffSecurity | None":
        try:
            return cls(
                frozenset(os.environ["STAFF_TRUSTED_ORIGINS"].split(",")),
                Path(os.environ["STAFF_CSRF_SECRET_FILE"]).read_text().strip(),
            )
        except (KeyError, OSError, ValueError):
            return None

    def issue(self, binding: str, purpose: str) -> str:
        return self._signer(purpose).dumps(token_hash(binding))

    def verify(self, token: str, binding: str, purpose: str, max_age: int) -> bool:
        if not token or not binding or len(token) > 512:
            return False
        try:
            actual = self._signer(purpose).loads(token, max_age=max_age)
        except BadData:
            return False
        return isinstance(actual, str) and hmac.compare_digest(
            actual, token_hash(binding)
        )

    def _signer(self, purpose: str) -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(
            self.secret,
            salt=f"staff-csrf-v1:{purpose}",
            signer_kwargs={"digest_method": hashlib.sha256},
        )
