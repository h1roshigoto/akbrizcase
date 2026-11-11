"""Independent order snapshots share catalog read locks through commit/rollback."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from pytest import MonkeyPatch
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_order_creation import (
    commercial,
    counts,
    creation_client,
    creation_db,
    order_headers,
)

from app.core.db import Database
from app.core.rate_limits import IpSecurity, RateLimitBucket
from app.modules.orders import router
from app.modules.orders.security import OrderSecurity
from app.modules.orders.service import create_order
from app.schemas.common import OrderHeaders
from app.schemas.orders import OrderCreate, OrderReceipt

pytestmark = pytest.mark.integration
__all__ = ["creation_client", "creation_db"]


@pytest.mark.parametrize("rollback_holder", [False, True])
def test_same_item_creations_overlap_without_leaking_failed_writes(
    creation_client: TestClient,
    creation_db: Database,
    monkeypatch: MonkeyPatch,
    rollback_holder: bool,
) -> None:
    body = commercial(creation_db, ("DEMO-S-001",))
    first_headers, second_headers = order_headers(), order_headers()
    held_key = UUID(first_headers["Idempotency-Key"])
    entered, release = Event(), Event()
    original = create_order

    def held(
        session: Session,
        request: OrderCreate,
        headers: OrderHeaders,
        security: OrderSecurity,
        chat_token: str | None = None,
    ) -> OrderReceipt:
        # Execute the real complete service write set before pausing; only
        # the HTTP UoW may commit. No catalog/quota/security method is stubbed.
        result = original(session, request, headers, security, chat_token)
        if headers.idempotency_key == held_key:
            entered.set()
            assert release.wait(5)
            if rollback_holder:
                raise OperationalError("synthetic", {}, Exception("synthetic rollback"))
        return result

    monkeypatch.setattr(router, "create_order", held)

    def submit(headers: dict[str, str]) -> Response:
        return creation_client.post("/api/v1/orders", json=body, headers=headers)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(submit, first_headers)
        try:
            assert entered.wait(3)
            second = executor.submit(submit, second_headers).result(2)
            assert second.status_code == 201, second.text
            assert counts(creation_db) == (1, 1, 1, 1, 1)
        finally:
            release.set()
        first_response = first.result(3)
    assert first_response.status_code == (503 if rollback_holder else 201)
    expected = 1 if rollback_holder else 2
    assert counts(creation_db) == (expected,) * 5

    # Replay and denied requests use the same item after both transactions end.
    replay = creation_client.post("/api/v1/orders", json=body, headers=second_headers)
    assert replay.status_code == 200 and replay.json() == second.json()
    denied = creation_client.post(
        "/api/v1/orders",
        json=body,
        headers=second_headers | {"X-Request-Token": "b" * 64},
    )
    assert denied.status_code == 404
    selections = body["items"]
    assert isinstance(selections, list)
    selected = selections[0]
    assert isinstance(selected, dict)
    stale = body | {"items": [selected | {"expected_revision": 999}]}
    assert (
        creation_client.post(
            "/api/v1/orders", json=stale, headers=order_headers()
        ).status_code
        == 409
    )
    assert counts(creation_db) == (expected,) * 5
    with creation_db.unit_of_work() as session:
        # Rotation aliases have the same committed count; denied/rolled-back
        # requests and replay consume no quota.
        security = IpSecurity.from_environment()
        assert security is not None
        epoch = session.scalar(
            text("SELECT floor(extract(epoch FROM clock_timestamp()) / 3600)::bigint")
        )
        assert isinstance(epoch, int)
        subjects = security.subjects("order", "192.0.2.1", epoch)
        assert set(
            session.scalars(
                select(RateLimitBucket.used).where(
                    RateLimitBucket.scope == "order",
                    RateLimitBucket.subject_hash.in_(subjects),
                )
            )
        ) == {expected}
