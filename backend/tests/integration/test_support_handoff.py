"""MVP-036: real PostgreSQL ownership, replay, rollback and independent comment."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from time import monotonic
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_order_creation import counts, erase_fixture, order_headers, support_body
from test_order_creation import creation_client as creation_client
from test_order_creation import creation_db as creation_db

from app.core.db import Database
from app.core.db_types import utc_now
from app.modules.chat.models import ChatSession
from app.modules.orders.models import ConsentText, Order

pytestmark = pytest.mark.integration


def own_request(client: TestClient) -> tuple[dict[str, object], dict[str, str]]:
    owner = client.post("/api/v1/chat/sessions", json={"mode": "diagnose"}).json()
    return (
        support_body() | {"source": "chat", "chat_session_id": owner["session_id"]},
        order_headers() | {"X-Chat-Token": owner["session_token"]},
    )


def expire(database: Database, identifier: object) -> None:
    before = utc_now() - timedelta(days=2)
    with database.unit_of_work() as session:
        session.execute(
            update(ChatSession)
            .where(ChatSession.id == UUID(str(identifier)))
            .values(
                created_at=before,
                last_activity_at=before,
                expires_at=before + timedelta(days=1),
            )
        )


@pytest.mark.parametrize("denial", ["wrong", "foreign", "absent", "expired"])
def test_owner_denial_has_no_writes(
    creation_client: TestClient, creation_db: Database, denial: str
) -> None:
    body, headers = own_request(creation_client)
    if denial == "wrong":
        headers["X-Chat-Token"] = "f" * 64
    elif denial == "foreign":
        _, other = own_request(creation_client)
        headers["X-Chat-Token"] = other["X-Chat-Token"]
    elif denial == "absent":
        body["chat_session_id"] = str(uuid4())
    else:
        expire(creation_db, body["chat_session_id"])
    response = creation_client.post("/api/v1/orders", json=body, headers=headers)
    assert response.status_code == (410 if denial == "expired" else 404)
    assert response.json()["error"]["details"] == {}
    assert response.headers["cache-control"] == "no-store"
    assert counts(creation_db) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize("removed", [False, True])
def test_replay_after_expiry_or_deletion(
    creation_client: TestClient, creation_db: Database, removed: bool
) -> None:
    body, headers = own_request(creation_client)
    body["comment"] = "  SYNTHETIC-036 edited <script>literal</script>\nexact text  "
    first = creation_client.post("/api/v1/orders", json=body, headers=headers)
    assert first.status_code == 201
    expire(creation_db, body["chat_session_id"])
    if removed:
        with creation_db.unit_of_work() as session:
            session.execute(
                delete(ChatSession).where(
                    ChatSession.id == UUID(str(body["chat_session_id"]))
                )
            )
    with creation_db.unit_of_work() as session:
        order = session.scalars(select(Order)).one()
        assert order.comment == body["comment"]
        assert order.source == "chat"
        assert (order.chat_session_id is None) is removed
    replay = creation_client.post("/api/v1/orders", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    # The order secret authenticates replay, not the old chat credential.
    assert (
        creation_client.post(
            "/api/v1/orders", json=body, headers=headers | {"X-Chat-Token": "f" * 64}
        ).status_code
        == 200
    )
    assert (
        creation_client.post(
            "/api/v1/orders", json=body | {"comment": "changed"}, headers=headers
        ).status_code
        == 409
    )
    assert (
        creation_client.post(
            "/api/v1/orders", json=body, headers=headers | {"X-Request-Token": "b" * 64}
        ).status_code
        == 404
    )
    assert counts(creation_db) == (1, 1, 0, 1, 1)
    # New creation still checks the old session, while site is independent.
    assert creation_client.post(
        "/api/v1/orders", json=body, headers=headers | order_headers()
    ).status_code == (404 if removed else 410)
    site = dict(body, source="site")
    site.pop("chat_session_id")
    assert (
        creation_client.post(
            "/api/v1/orders", json=site, headers=order_headers()
        ).status_code
        == 201
    )
    erase_fixture(creation_db)
    assert (
        creation_client.post("/api/v1/orders", json=body, headers=headers).status_code
        == 404
    )
    with creation_db.unit_of_work() as session:
        assert all(row.comment is None for row in session.scalars(select(Order)))


def test_concurrent_chat_insertions(
    creation_client: TestClient, creation_db: Database
) -> None:
    body, headers = own_request(creation_client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(
            pool.map(
                lambda _: (
                    creation_client.post(
                        "/api/v1/orders", json=body, headers=headers
                    ).status_code
                ),
                range(4),
            )
        )
    assert sorted(statuses) == [200, 200, 200, 201]
    assert counts(creation_db) == (1, 1, 0, 1, 1)


@pytest.mark.parametrize("blocker", ["owner", "consent"])
def test_expiry_after_real_lock_wait_rolls_back(
    creation_client: TestClient,
    creation_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
) -> None:
    body, headers = own_request(creation_client)
    identifier = UUID(str(body["chat_session_id"]))
    from app.modules.chat import access
    from app.modules.orders import service

    with creation_db.unit_of_work() as session:
        expires = session.scalar(
            select(ChatSession.expires_at).where(ChatSession.id == identifier)
        )
    assert expires is not None
    with ThreadPoolExecutor(max_workers=1) as pool:
        with creation_db.unit_of_work() as locker:
            if blocker == "owner":
                locker.scalar(
                    select(ChatSession)
                    .where(ChatSession.id == identifier)
                    .with_for_update()
                )
            else:
                locker.scalar(select(ConsentText).with_for_update())
            pending = pool.submit(
                creation_client.post, "/api/v1/orders", json=body, headers=headers
            )
            deadline = monotonic() + 10
            waiting = False
            while monotonic() < deadline and not waiting:
                with creation_db.engine.connect() as observer:
                    waiting = bool(
                        observer.scalar(
                            text(
                                "SELECT EXISTS(SELECT FROM pg_stat_activity "
                                "WHERE datname=current_database() "
                                "AND state='active' "
                                "AND cardinality(pg_blocking_pids(pid))>0)"
                            )
                        )
                    )
            assert waiting, "Creation must actually wait on a PostgreSQL lock"
            monkeypatch.setattr(
                access if blocker == "owner" else service,
                "utc_now",
                lambda: expires + timedelta(seconds=1),
            )
        assert pending.result(timeout=10).status_code == 410
    assert counts(creation_db) == (0, 0, 0, 0, 0)


def test_failure_after_order_flush_rolls_back_everything(
    creation_client: TestClient, creation_db: Database
) -> None:
    body, headers = own_request(creation_client)

    def fail(session: Session, *_: object) -> None:
        if any(isinstance(row, Order) for row in session.new):
            raise OperationalError("synthetic precommit failure", None, Exception())

    event.listen(Session, "after_flush", fail)
    try:
        assert (
            creation_client.post(
                "/api/v1/orders", json=body, headers=headers
            ).status_code
            == 503
        )
    finally:
        event.remove(Session, "after_flush", fail)
    assert counts(creation_db) == (0, 0, 0, 0, 0)
    assert (
        creation_client.post("/api/v1/orders", json=body, headers=headers).status_code
        == 201
    )
