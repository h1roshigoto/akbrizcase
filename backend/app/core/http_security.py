"""Only a resolved, explicit Nginx peer may assert the client address.

Uvicorn stays --no-proxy-headers. Never use Forwarded/X-Forwarded-* to derive an
address, origin or scheme. Compose publishes only Nginx, with no private volumes.
"""

import ipaddress
import os
import socket
from dataclasses import dataclass

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.rate_limits import IpSecurity


@dataclass(frozen=True)
class TrustedProxy:
    addresses: frozenset[str] = frozenset()
    resolve_nginx: bool = False

    @classmethod
    def from_environment(cls) -> "TrustedProxy":
        host = os.environ.get("TRUSTED_PROXY_HOST")
        if host is None:
            return cls()
        # Exactly one deployment-owned DNS name, never a client value or CIDR.
        if host != "nginx":
            return cls()
        try:
            return cls(
                frozenset(
                    str(ipaddress.ip_address(row[4][0]))
                    for row in socket.getaddrinfo(host, None)
                ),
                resolve_nginx=True,
            )
        except (OSError, ValueError):
            return cls(resolve_nginx=True)  # No trust until deployment DNS resolves.

    def trusted_addresses(self) -> frozenset[str]:
        if not self.resolve_nginx:
            return self.addresses
        # Re-resolve to handle the API/Nginx startup dependency and replacements;
        # never keep trusting an address released to another container.
        try:
            return frozenset(
                str(ipaddress.ip_address(row[4][0]))
                for row in socket.getaddrinfo("nginx", None)
            )
        except (OSError, ValueError):
            return frozenset()

    def external_origin(self, request: Request) -> str:
        scheme = str(request.scope["scheme"])
        if request.client and request.client.host in self.trusted_addresses():
            values = request.headers.getlist("x-akbriz-scheme")
            if len(values) != 1 or values[0] not in {"http", "https"}:
                raise HTTPException(503)
            scheme = values[0]
        return scheme + "://" + request.headers.get("host", "")

    def client_address(self, request: Request) -> str:
        try:
            if request.client is None:
                raise ValueError
            peer = str(ipaddress.ip_address(request.client.host))
            if peer in self.trusted_addresses():
                headers = request.headers.getlist("x-akbriz-client-ip")
                if len(headers) != 1:
                    raise ValueError
                return str(ipaddress.ip_address(headers[0]))
            return peer
        except ValueError:
            raise HTTPException(503) from None


def ip_subjects(request: Request, scope: str, session: Session) -> tuple[str, ...]:
    security: IpSecurity | None = getattr(request.app.state, "ip_security", None)
    proxy: TrustedProxy = getattr(request.app.state, "trusted_proxy", TrustedProxy())
    if security is None:
        raise HTTPException(503)
    epoch = session.scalar(
        text("SELECT floor(extract(epoch FROM clock_timestamp()) / 3600)::bigint")
    )
    assert isinstance(epoch, int)
    return security.subjects(scope, proxy.client_address(request), epoch)
