"""MVP-039 real HTTPS browser and HTTP/Nginx acceptance, synthetic only."""

import json
import math
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from urllib.error import HTTPError
from urllib.request import Request
from uuid import UUID

from https_smoke import main


def edge_boundary(opener, origin, http_origin):
    """An untouched real Nginx zone admits its approved initial burst of 120.

    Account for actual token recovery during transport, rather than pretending a
    leaky bucket implements a calendar-minute counter. Both listeners share it.
    """
    def request(index):
        target = origin if index % 2 else http_origin
        request = Request(target + "/api/v1/synthetic-limit-probe", headers={
            "X-Forwarded-For": f"198.51.100.{index + 1}",
            "Forwarded": "for=203.0.113.1",
        })
        try:
            response = opener.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            if response.status == 429:
                body = json.load(response)
                assert body["error"] == {"code": "RATE_LIMITED", "message": "Too many requests", "details": {}}
                assert str(UUID(body["request_id"])) == response.headers["X-Request-ID"]
                assert response.headers["Retry-After"] == "1"
                assert response.headers["Cache-Control"] == "no-store"
            else:
                assert response.status == 404
            return response.status

    started = monotonic()
    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(request, range(128)))
    elapsed = monotonic() - started
    accepted = results.count(404)
    assert 120 <= accepted <= 120 + math.ceil(elapsed * 2)
    assert 429 in results
    print("PASS: real shared HTTP/HTTPS 120-request Nginx burst and 120r/m recovery, spoofed headers ignored")


if __name__ == "__main__":
    main(http_security=True)
