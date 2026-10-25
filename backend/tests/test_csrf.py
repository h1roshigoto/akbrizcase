"""CSRF primitives and strict configuration without database substitutes."""

import secrets

import pytest
from itsdangerous import TimestampSigner

from app.core.csrf import StaffSecurity, https_origin
from app.core.staff_sessions import valid_token


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "null",
        "http://localhost",
        "https://localhost/",
        "https://localhost/path",
        "https://user@localhost",
        "https://localhost?x=1",
        "https://localhost#x",
        "https://localhost:bad",
        "https://localhost https://evil.test",
    ],
)
def test_invalid_origins(origin: str) -> None:
    assert not https_origin(origin)


def test_tokens_are_bound_timed_and_purpose_separated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = StaffSecurity(
        frozenset({"https://localhost:8443"}), secrets.token_hex(32)
    )
    binding = secrets.token_hex(32)
    token = security.issue(binding, "login")
    assert security.verify(token, binding, "login", 600)
    assert not security.verify(token, secrets.token_hex(32), "login", 600)
    assert not security.verify(token, binding, "session", 600)
    assert not security.verify(token + "x", binding, "login", 600)
    assert not security.verify("", binding, "login", 600)
    assert not security.verify(token, "", "login", 600)
    timestamp = TimestampSigner.get_timestamp
    monkeypatch.setattr(
        TimestampSigner, "get_timestamp", lambda self: timestamp(self) + 601
    )
    assert not security.verify(token, binding, "login", 600)


def test_session_token_shape() -> None:
    assert valid_token(secrets.token_hex(32))
    assert not valid_token("a" * 63)
    assert not valid_token("g" * 64)
