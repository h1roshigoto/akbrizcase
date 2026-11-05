"""MVP-030 public ownership and fail-closed history on real PostgreSQL 16."""

import json
import logging
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from pytest import MonkeyPatch
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.orm import Session

from app.core.csrf import SESSION_COOKIE, StaffSecurity
from app.core.db import Database
from app.core.staff import bootstrap_admin
from app.main import app
from app.modules.chat.access import ChatExpired, owner_token_hash, require_owner
from app.modules.chat.models import ChatMessage, ChatSession
from app.schemas.chat import ChatSessionCreated, ChatSessionRead

pytestmark = pytest.mark.integration
ROOT = "/api/v1/chat/sessions"
ORIGIN = "https://localhost:8443"


@pytest.fixture
def chat_db(staff_database: Database) -> Iterator[Database]:
    try:
        yield staff_database
    finally:
        with staff_database.unit_of_work() as session:
            session.execute(delete(ChatSession))


@pytest.fixture
def browsers(chat_db: Database) -> Iterator[tuple[TestClient, TestClient]]:
    with (
        TestClient(app, client=("192.0.2.1", 50000), base_url=ORIGIN) as one,
        TestClient(app, client=("192.0.2.1", 50000), base_url=ORIGIN) as two,
    ):
        app.state.database = chat_db
        app.state.staff_security = StaffSecurity(
            frozenset({ORIGIN}), secrets.token_hex(32)
        )
        yield one, two


def create(browser: TestClient, mode: str = "diagnose") -> dict[str, Any]:
    response = browser.post(ROOT, json={"mode": mode})
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers and "set-cookie" not in response.headers
    ChatSessionCreated.model_validate(response.json())
    body: dict[str, Any] = response.json()
    return body


def read(browser: TestClient, body: dict[str, Any], **kwargs: Any) -> Response:
    return browser.get(
        f"{ROOT}/{body['session_id']}",
        headers={"X-Chat-Token": body["session_token"]},
        **kwargs,
    )


def assert_error(response: Response, status: int = 404) -> dict[str, Any]:
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["request_id"] == response.headers["x-request-id"]
    assert UUID(payload["request_id"]).version == 4
    error: dict[str, Any] = payload["error"]
    assert error == {
        "code": "NOT_FOUND" if status == 404 else "SESSION_EXPIRED",
        "message": "Object not found" if status == 404 else "Session expired",
        "details": {},
    }
    return error


@pytest.mark.parametrize("mode", ["diagnose", "knowledge", "commerce"])
def test_create_hash_once_and_read_without_sliding(
    browsers: tuple[TestClient, TestClient], chat_db: Database, mode: str
) -> None:
    one, _ = browsers
    body = create(one, mode)
    with chat_db.unit_of_work() as session:
        row = session.get(ChatSession, UUID(body["session_id"]))
        assert row is not None
        assert row.owner_token_hash == owner_token_hash(body["session_token"])
        assert row.expires_at - row.created_at == timedelta(hours=24)
        assert row.last_activity_at == row.created_at
        original = (row.created_at, row.last_activity_at, row.expires_at)
        assert row.state_version == 0 and row.tree_id is None and row.outcome is None
        assert session.scalar(select(func.count()).select_from(ChatMessage)) == 0
    for _ in range(2):
        response = read(one, body)
        assert response.status_code == 200
        result = ChatSessionRead.model_validate(response.json())
        assert result.mode == mode and result.state_version == 0
        assert result.messages == [] and result.next_before is None
        assert result.reply == ChatSessionCreated.model_validate(body).reply
        assert result.reply.resolution == "no_match"
        assert {b.input.type for b in result.reply.buttons} == {"select_mode"}
        assert {b.input.mode for b in result.reply.buttons} == {  # type: ignore[union-attr]
            "diagnose",
            "knowledge",
            "commerce",
        }
        assert response.headers["cache-control"] == "no-store"
        assert body["session_token"] not in response.text + str(response.headers)
        assert "owner_token_hash" not in response.text
    with chat_db.unit_of_work() as session:
        row = session.get(ChatSession, UUID(body["session_id"]))
        assert (
            row and (row.created_at, row.last_activity_at, row.expires_at) == original
        )


def test_two_independent_owners_and_uniform_denials(
    browsers: tuple[TestClient, TestClient], chat_db: Database
) -> None:
    one, two = browsers
    first, second = create(one), create(two)
    assert first["session_id"] != second["session_id"]
    assert first["session_token"] != second["session_token"]
    assert not one.cookies and not two.cookies
    for browser, own, other in ((one, first, second), (two, second, first)):
        assert read(browser, own).status_code == 200
        path = f"{ROOT}/{other['session_id']}"
        denied = [
            browser.get(path),
            browser.get(path, headers={"X-Chat-Token": own["session_token"]}),
            browser.get(path, headers={"X-Chat-Token": secrets.token_hex(32)}),
            browser.get(
                f"{ROOT}/{uuid4()}", headers={"X-Chat-Token": own["session_token"]}
            ),
            browser.get(path, headers={"X-Chat-Token": "malformed-secret"}),
            browser.get(path, headers={"X-Chat-Token": "A" * 64}),
            browser.get(path, headers={"X-Chat-Token": "x" * 4096}),
            browser.get(
                path,
                headers=[("X-Chat-Token", other["session_token"])] * 2,
            ),
            browser.get(
                path, headers={"Authorization": "Bearer " + other["session_token"]}
            ),
        ]
        assert all(
            assert_error(response) == denied[0].json()["error"] for response in denied
        )
    with chat_db.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(ChatSession)) == 2
        assert session.scalar(select(func.count()).select_from(ChatMessage)) == 0


@pytest.mark.parametrize("delta,status", [(-1, 200), (0, 410), (1, 410)])
def test_expiry_boundary_owner_checked_first(
    browsers: tuple[TestClient, TestClient],
    chat_db: Database,
    monkeypatch: MonkeyPatch,
    delta: int,
    status: int,
) -> None:
    one, two = browsers
    body = create(one)
    with chat_db.unit_of_work() as session:
        row = session.get(ChatSession, UUID(body["session_id"]))
        assert row
        expiry = row.expires_at
    now = expiry + timedelta(microseconds=delta)
    monkeypatch.setattr("app.modules.chat.access.utc_now", lambda: now)
    response = read(one, body)
    assert response.status_code == status
    if status == 410:
        assert_error(response, 410)
    assert_error(two.get(f"{ROOT}/{body['session_id']}"))
    assert_error(
        two.get(
            f"{ROOT}/{body['session_id']}",
            headers={"X-Chat-Token": secrets.token_hex(32)},
        )
    )


def add_messages(database: Database, identifier: str, count: int) -> list[UUID]:
    # Equal timestamps exercise the UUID tie-breaker; records span three groups.
    ids = [uuid4() for _ in range(count)]
    with database.unit_of_work() as session:
        for index, message_id in enumerate(ids):
            session.add(
                ChatMessage(
                    id=message_id,
                    session_id=UUID(identifier),
                    request_id=uuid4(),
                    request_hash=secrets.token_hex(32),
                    input={"type": "text", "value": "SYNTHETIC PRIVATE INPUT"},
                    response={
                        "text": "SYNTHETIC UNSAFE TECHNICAL RESPONSE",
                        "mode": "diagnose",
                        "sources": [
                            {"section_id": str(uuid4()), "visibility": "internal"}
                        ],
                        "items": [{"price": "1.00", "status": "archived"}],
                        "actions": [
                            {
                                "type": "open_document",
                                "payload": {"url": "https://invalid.example"},
                            }
                        ],
                        "buttons": [{"id": "unsafe", "label": "PRIVATE"}],
                        "resolution": "completed",
                        "session_token": "SYNTHETIC POISONED SECRET",
                    },
                    state_version_after=index + 1,
                    matched_by="none",
                    latency_ms=0,
                    created_at=datetime(2026, 1, 1, tzinfo=UTC)
                    + timedelta(seconds=index // 40),
                )
            )
        session.execute(
            update(ChatSession)
            .where(ChatSession.id == UUID(identifier))
            .values(state_version=count)
        )
    return [
        identifier
        for _, identifier in sorted(
            (index // 40, value) for index, value in enumerate(ids)
        )
    ]


@pytest.mark.parametrize("count", [0, 1, 50, 51, 123])
def test_pagination_and_safe_projection(
    browsers: tuple[TestClient, TestClient], chat_db: Database, count: int
) -> None:
    one, _ = browsers
    body = create(one)
    expected = add_messages(chat_db, body["session_id"], count)
    seen: list[UUID] = []
    params: dict[str, str] = {}
    while True:
        response = read(one, body, params=params)
        assert response.status_code == 200
        result = ChatSessionRead.model_validate(response.json())
        assert result.state_version == count
        assert len(result.messages) <= 50
        seen = [exchange.message_id for exchange in result.messages] + seen
        for exchange in result.messages:
            assert exchange.reply == result.reply
            assert exchange.reply.sources == []
            assert exchange.reply.items == []
            assert exchange.reply.buttons == []
            assert exchange.reply.resolution == "handoff"
        for private in (
            "PRIVATE",
            "UNSAFE",
            "POISONED",
            "section_id",
            "archived",
            "request_hash",
            "latency_ms",
            "response",
        ):
            assert private not in response.text
        # Public buttons have a server-generated input. Raw stored message input
        # must remain absent from the session and exchange objects themselves.
        assert "input" not in response.json()
        assert all("input" not in m for m in response.json()["messages"])
        if result.next_before is None:
            break
        assert len(result.messages) == 50
        assert result.next_before == result.messages[0].message_id
        params = {"before": str(result.next_before)}
    assert seen == expected
    if expected:
        assert (
            read(one, body, params={"before": str(expected[0])}).json()["messages"]
            == []
        )


def test_cursor_is_scoped_and_checked_after_ownership(
    browsers: tuple[TestClient, TestClient], chat_db: Database
) -> None:
    one, two = browsers
    first, second = create(one), create(two)
    foreign = add_messages(chat_db, second["session_id"], 1)[0]
    for cursor in (foreign, uuid4()):
        assert_error(read(one, first, params={"before": str(cursor)}))
        assert_error(
            one.get(f"{ROOT}/{second['session_id']}", params={"before": str(cursor)})
        )
    response = read(one, first, params={"before": "SYNTHETIC-INVALID"})
    assert response.status_code == 422 and "SYNTHETIC-INVALID" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_valid_admin_cookie_cannot_read_foreign_chat_or_change_projection(
    browsers: tuple[TestClient, TestClient], chat_db: Database
) -> None:
    owner, admin = browsers
    password = "synthetic-chat-admin-password"
    with chat_db.unit_of_work() as session:
        assert bootstrap_admin(session, login="chat-admin", password=password).ok
    csrf = admin.get("/admin/auth/csrf").json()["csrf_token"]
    assert (
        admin.post(
            "/admin/auth/login",
            json={"login": "chat-admin", "password": password},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        ).status_code
        == 200
    )
    assert SESSION_COOKIE in admin.cookies
    assert admin.get("/api/v1/admin/orders").status_code == 200
    body = create(owner)
    add_messages(chat_db, body["session_id"], 1)
    assert_error(admin.get(f"{ROOT}/{body['session_id']}"))
    assert_error(
        admin.get(
            f"{ROOT}/{body['session_id']}",
            headers={"X-Chat-Token": secrets.token_hex(32)},
        )
    )
    assert read(admin, body).json() == read(owner, body).json()


def test_refresh_reused_session_checks_expiry(
    browsers: tuple[TestClient, TestClient], chat_db: Database
) -> None:
    body = create(browsers[0])
    with chat_db.sessions() as session:
        row = require_owner(session, UUID(body["session_id"]), body["session_token"])
        session.commit()
        with chat_db.unit_of_work() as another:
            created = row.created_at - timedelta(hours=25)
            another.execute(
                update(ChatSession)
                .where(ChatSession.id == row.id)
                .values(
                    created_at=created,
                    last_activity_at=created,
                    expires_at=created + timedelta(hours=24),
                )
            )
        with pytest.raises(ChatExpired):
            require_owner(session, row.id, body["session_token"])


def test_query_or_cookie_token_does_not_authenticate(
    browsers: tuple[TestClient, TestClient],
) -> None:
    owner, other = browsers
    body = create(owner)
    path = f"{ROOT}/{body['session_id']}"
    for name in ("token", "session_token", "X-Chat-Token"):
        assert_error(other.get(path, params={name: body["session_token"]}))
        other.cookies.set(name, body["session_token"])
    assert_error(other.get(path))
    other.cookies.clear()


def test_history_database_failure_is_sanitized(
    browsers: tuple[TestClient, TestClient], databases: tuple[Database, Database]
) -> None:
    owner, other = browsers
    body = create(owner)
    _, migrator = databases
    try:
        with migrator.engine.begin() as connection:
            connection.execute(
                text("REVOKE SELECT ON app.chat_messages FROM akbrizcase_runtime")
            )
        response = read(owner, body)
        assert response.status_code == 503
        assert response.json()["error"] == {
            "code": "SERVICE_UNAVAILABLE",
            "message": "Service temporarily unavailable",
            "details": {},
        }
        assert response.headers["cache-control"] == "no-store"
        assert body["session_token"] not in response.text
        assert "chat_messages" not in response.text
        assert_error(other.get(f"{ROOT}/{body['session_id']}"))
    finally:
        with migrator.engine.begin() as connection:
            connection.execute(
                text("GRANT SELECT ON app.chat_messages TO akbrizcase_runtime")
            )


def test_failure_rolls_back_and_does_not_disclose_token(
    browsers: tuple[TestClient, TestClient],
    chat_db: Database,
    monkeypatch: MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    issued = secrets.token_hex(32)
    monkeypatch.setattr(
        "app.modules.chat.service.secrets.token_hex", lambda count: issued
    )

    def fail_commit(session: Session) -> None:
        session.execute(text("SELECT 1 / 0"))

    event.listen(Session, "before_commit", fail_commit)
    try:
        response = browsers[0].post(ROOT, json={"mode": "knowledge"})
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
    finally:
        event.remove(Session, "before_commit", fail_commit)
    with chat_db.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(ChatSession)) == 0
    output = capsys.readouterr()
    assert (
        issued
        not in response.text
        + str(response.headers)
        + caplog.text
        + output.out
        + output.err
    )


def test_token_absent_from_storage_logs_and_history(
    browsers: tuple[TestClient, TestClient],
    chat_db: Database,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    one, two = browsers
    body = create(one)
    response = read(one, body)
    denied = two.get(
        f"{ROOT}/{body['session_id']}", headers={"X-Chat-Token": secrets.token_hex(32)}
    )
    assert_error(denied)
    with chat_db.unit_of_work() as session:
        saved = str(
            session.execute(
                text("SELECT row_to_json(s) FROM app.chat_sessions s")
            ).scalar_one()
        )
        history = str(session.execute(text("SELECT * FROM app.chat_messages")).all())
    output = capsys.readouterr()
    assert (
        body["session_token"]
        not in saved
        + history
        + caplog.text
        + output.out
        + output.err
        + response.text
        + denied.text
    )
    assert "session_token" not in json.dumps(response.json())
