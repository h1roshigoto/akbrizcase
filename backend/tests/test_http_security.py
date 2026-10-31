"""Trust decisions use the transport peer, with no ambient proxy-header trust."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from starlette.requests import Request

from app.core.http_security import TrustedProxy
from app.core.rate_limits import IpSecurity
from app.main import app
from app.modules.orders.security import OrderSecurity


def incoming(peer: str, headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        dict(
            type="http",
            scheme="http",
            server=("backend", 8000),
            client=(peer, 50000),
            method="POST",
            path="/api/v1/orders",
            headers=headers,
        )
    )


@pytest.mark.parametrize("peer", ["192.0.2.10", "2001:db8::1"])
def test_direct_peer_ignores_every_claim(peer: str) -> None:
    request = incoming(
        peer,
        [
            (b"forwarded", b"for=198.51.100.1;proto=https"),
            (b"x-forwarded-for", b"198.51.100.2"),
            (b"x-real-ip", b"198.51.100.3"),
            (b"x-akbriz-client-ip", b"198.51.100.4"),
            (b"x-akbriz-scheme", b"https"),
            (b"host", b"backend"),
        ],
    )
    proxy = TrustedProxy(frozenset({"192.0.2.11"}))
    assert proxy.client_address(request) == peer
    assert proxy.external_origin(request) == "http://backend"


@pytest.mark.parametrize(
    "values",
    [[], [b"bad"], [b"1.2.3.4, 5.6.7.8"], [b"1.2.3.4", b"5.6.7.8"], [b"unknown"]],
)
def test_trusted_peer_requires_one_literal_address(values: list[bytes]) -> None:
    proxy = TrustedProxy(frozenset({"192.0.2.11"}))
    with pytest.raises(HTTPException) as raised:
        proxy.client_address(
            incoming("192.0.2.11", [(b"x-akbriz-client-ip", v) for v in values])
        )
    assert raised.value.status_code == 503


def test_trusted_peer_uses_only_its_dedicated_assertion() -> None:
    proxy = TrustedProxy(frozenset({"192.0.2.11"}))
    request = incoming(
        "192.0.2.11",
        [
            (b"x-akbriz-client-ip", b"2001:0db8::1"),
            (b"x-akbriz-scheme", b"https"),
            (b"host", b"localhost:8443"),
            (b"x-forwarded-for", b"198.51.100.2"),
        ],
    )
    assert proxy.client_address(request) == "2001:db8::1"
    assert proxy.external_origin(request) == "https://localhost:8443"


def test_dns_failure_and_unconfigured_proxy_never_trust_networks(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRUSTED_PROXY_HOST", raising=False)
    assert not TrustedProxy.from_environment().addresses
    monkeypatch.setenv("TRUSTED_PROXY_HOST", "0.0.0.0/0")
    assert not TrustedProxy.from_environment().addresses
    monkeypatch.setenv("TRUSTED_PROXY_HOST", "nginx")
    with patch("socket.getaddrinfo", side_effect=OSError):
        assert not TrustedProxy.from_environment().addresses
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.0.2.11", 0))]):
        assert TrustedProxy.from_environment().addresses == {"192.0.2.11"}


def test_ip_hashes_are_separate_bounded_rotatable_and_secret_free(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    old = OrderSecurity(b"synthetic-ip-secret" * 3)
    rotated = OrderSecurity(b"synthetic-new-ip-secret" * 3, (old.secret,))
    original, current = IpSecurity(old), IpSecurity(rotated)
    assert set(original.subjects("order", "192.0.2.1", 100)) < set(
        current.subjects("order", "192.0.2.1", 100)
    )
    assert original.subjects("order", "192.0.2.1", 100) != original.subjects(
        "session", "192.0.2.1", 100
    )
    assert "synthetic" not in repr(current)
    monkeypatch.delenv("IP_HMAC_SECRET_FILE", raising=False)
    assert IpSecurity.from_environment() is None
    monkeypatch.setenv("IP_HMAC_SECRET", "forbidden-inline-secret")
    assert IpSecurity.from_environment() is None
    path = tmp_path / "key"
    path.write_text("short")
    monkeypatch.setenv("IP_HMAC_SECRET_FILE", str(path))
    assert IpSecurity.from_environment() is None
    path.write_bytes(old.secret)
    assert IpSecurity.from_environment() == original


@pytest.fixture
def browser(monkeypatch: MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.delenv("DB_PASSWORD_FILE", raising=False)
    with TestClient(app, base_url="https://example.test") as client:
        yield client


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.test",
        "null",
        "http://example.test",
        "https://example.test.evil.test",
    ],
)
def test_cross_origin_denied_without_cors_permission(
    browser: TestClient, origin: str
) -> None:
    response = browser.post(
        "/api/v1/chat/sessions", json={"mode": "diagnose"}, headers={"Origin": origin}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORIGIN_DENIED"
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers
    assert origin not in response.text


def test_same_origin_and_nonbrowser_reach_normal_validation(
    browser: TestClient,
) -> None:
    for headers in ({}, {"Origin": "https://example.test"}):
        response = browser.post(
            "/api/v1/chat/sessions", json={"mode": "invalid"}, headers=headers
        )
        assert response.status_code == 422
    response = browser.options(
        "/api/v1/chat/sessions",
        headers={
            "Origin": "https://evil.test",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_csp_no_third_party_script_or_inline_permission(browser: TestClient) -> None:
    first = browser.get("/healthz")
    second = browser.get("/api/v1/not-found")
    for response in (first, second):
        csp = response.headers["content-security-policy"]
        script = csp.split("script-src ")[1].split(";")[0]
        assert script.startswith("'self' 'nonce-")
        assert "unsafe-inline" not in script and "http" not in script
        assert "object-src 'none'" in csp and "frame-ancestors 'none'" in csp
        assert response.headers["x-content-type-options"] == "nosniff"
    assert (
        first.headers["content-security-policy"]
        != second.headers["content-security-policy"]
    )
    assert second.headers["cache-control"] == "no-store"
    assert browser.get("/docs").status_code == 404


def test_hourly_ip_aliases_overlap_once_and_cannot_link_indefinitely() -> None:
    security = IpSecurity(OrderSecurity(b"synthetic-ip-secret" * 3))
    first = set(security.subjects("order", "192.0.2.1", 100))
    second = set(security.subjects("order", "192.0.2.1", 101))
    third = set(security.subjects("order", "192.0.2.1", 102))
    assert len(first & second) == 1
    assert not first & third
