"""MVP-033 HTTP acceptance on PostgreSQL 16, independent transactions/locks."""

import json
import logging
import secrets
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from threading import Event, Lock
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import JsonValue
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_catalog_applicability import Corpus, corpus, wait_for_lock
from test_chat_sessions import ORIGIN, create
from test_diagnostic_trees import invoke, payload, write
from test_tree_lifecycle import prepared, run

from app.core.api_errors import install_api_errors
from app.core.csrf import SESSION_COOKIE, StaffSecurity
from app.core.db import Database, database_lifespan
from app.core.db_types import utc_now
from app.core.staff import bootstrap_admin
from app.main import app
from app.modules.chat.access import owner_token_hash
from app.modules.chat.message_request import request_hash
from app.modules.chat.message_state import decode_response
from app.modules.chat.mode_router import ModeRouter, RouterResult
from app.modules.chat.models import ChatMessage, ChatSession
from app.modules.chat.router import router as chat_router
from app.modules.kb.models import Document, DocumentVersion
from app.modules.kb.tree_models import DiagnosticTree
from app.schemas.chat import ChatMessageCreate, ChatMessageResult

pytestmark = pytest.mark.integration
URL = "/api/v1/chat/messages"


@pytest.fixture
def message_db(databases: tuple[Database, Database]) -> Iterator[Database]:
    try:
        yield databases[0]
    finally:
        # Published tree history is deliberately immutable. Keep the synthetic
        # trees/dependencies/audit in this disposable stack; never bypass their
        # triggers for teardown. Only transient chat exchanges need clearing.
        with databases[0].unit_of_work() as session:
            session.execute(delete(ChatSession))


@pytest.fixture
def browsers(message_db: Database) -> Iterator[tuple[TestClient, TestClient]]:
    with (
        TestClient(app, base_url=ORIGIN) as one,
        TestClient(app, base_url=ORIGIN) as two,
    ):
        app.state.database = message_db
        app.state.staff_security = StaffSecurity(
            frozenset({ORIGIN}), secrets.token_hex(32)
        )
        yield one, two


def body(own: dict[str, Any], version: int = 0, **input: Any) -> dict[str, Any]:
    return {
        "session_id": own["session_id"],
        "request_id": str(uuid4()),
        "expected_version": version,
        "input": input or {"type": "text", "value": "SYNTHETIC"},
    }


def post(browser: TestClient, own: dict[str, Any], request: dict[str, Any]) -> Any:
    return browser.post(
        URL, json=request, headers={"X-Chat-Token": own["session_token"]}
    )


def success(response: Any, version: int) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    result = ChatMessageResult.model_validate(response.json())
    assert result.state_version == version
    assert result.reply.resolution == "handoff"
    assert (
        not result.reply.sources and not result.reply.items and not result.reply.buttons
    )
    assert set(response.json()) == {"message_id", "state_version", "reply"}
    assert response.headers["cache-control"] == "no-store"
    assert UUID(response.headers["x-request-id"]).version == 4
    value: dict[str, Any] = response.json()
    return value


def error(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["details"] == {}
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"


def saved(
    database: Database, own: dict[str, Any]
) -> tuple[ChatSession, list[ChatMessage]]:
    with database.unit_of_work() as session:
        row = session.get(ChatSession, UUID(own["session_id"]))
        assert row
        records = list(
            session.scalars(
                select(ChatMessage)
                .where(ChatMessage.session_id == row.id)
                .order_by(ChatMessage.state_version_after)
            )
        )
        return row, records


def test_sequence_replay_before_stale_version_and_body_conflicts(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one, _ = browsers
    own = create(one)
    initial, _ = saved(message_db, own)
    first = body(own, type="select_mode", mode="knowledge")
    original = success(post(one, own, first), 1)
    success(post(one, own, body(own, 1)), 2)
    success(post(one, own, body(own, 2, type="select_mode", mode="commerce")), 3)
    before, records = saved(message_db, own)
    assert len(records) == before.state_version == 3
    assert before.mode == "commerce"
    assert before.expires_at == initial.expires_at
    assert before.last_activity_at >= initial.last_activity_at
    assert records[0].request_hash == request_hash(
        ChatMessageCreate.model_validate(first)
    )
    assert records[0].matched_by == "button"
    assert all(r.latency_ms >= 0 for r in records)

    def forbidden(*args: object, **kwargs: object) -> RouterResult:
        pytest.fail("Replay/conflicts must not route or step")

    monkeypatch.setattr(ModeRouter, "route", forbidden)
    # Object key order, JSON whitespace and transport request IDs are irrelevant.
    replay = one.post(
        URL,
        content=json.dumps(first, sort_keys=True),
        headers={
            "X-Chat-Token": own["session_token"],
            "X-Request-ID": str(uuid4()),
            "Content-Type": "application/json",
        },
    )
    assert success(replay, 1) == original
    for changed in (
        first | {"expected_version": 3},
        first | {"input": {"type": "select_mode", "mode": "commerce"}},
    ):
        error(post(one, own, changed), 409, "IDEMPOTENCY_CONFLICT")
    error(post(one, own, body(own, 0)), 409, "STATE_CONFLICT")
    after, remaining = saved(message_db, own)
    assert len(remaining) == after.state_version == 3
    assert after.last_activity_at == before.last_activity_at


@pytest.mark.parametrize("identical", [False, True])
def test_concurrent_http_requests_use_one_exclusive_lock(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    identical: bool,
    tmp_path: Path,
) -> None:
    one, two = browsers
    c, pin, key = scenario(message_db, tmp_path)
    own = create(one)
    success(post(one, own, start(own, c, key)), 1)
    first = body(own, 1, type="button", option_id="a")
    second = (
        deepcopy(first) if identical else body(own, 1, type="button", option_id="a")
    )
    entered, release = Event(), Event()
    route = ModeRouter.route
    calls = 0
    guard = Lock()

    def held(self: ModeRouter, *args: Any, **kwargs: Any) -> RouterResult:
        nonlocal calls
        with guard:
            calls += 1
        entered.set()
        assert release.wait(2)
        return route(self, *args, **kwargs)

    monkeypatch.setattr(ModeRouter, "route", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(post, one, own, first)
        try:
            assert entered.wait(2)
            loser = pool.submit(post, two, own, second)
            wait_for_lock(message_db)  # Observe a real PostgreSQL row-lock wait.
        finally:
            release.set()
        first_response, second_response = winner.result(5), loser.result(5)
    result = success(first_response, 2)
    if identical:
        assert success(second_response, 2) == result
    else:
        error(second_response, 409, "STATE_CONFLICT")
    row, records = saved(message_db, own)
    assert row.state_version == len(records) == 2 and calls == 1
    assert row.tree_id == pin and row.current_node_key == "result"
    assert decode_response(records[-1]).state_after.transitions == 1


def test_independent_sessions_do_not_block_each_other(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one, two = browsers
    first, second = create(one), create(two)
    entered, release = Event(), Event()
    route = ModeRouter.route

    def held(self: ModeRouter, *args: Any, **kwargs: Any) -> RouterResult:
        # Identify the first connection by the session row it has locked.
        if not entered.is_set():
            entered.set()
            assert release.wait(2)
        return route(self, *args, **kwargs)

    monkeypatch.setattr(ModeRouter, "route", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(post, one, first, body(first))
        try:
            assert entered.wait(2)
            success(pool.submit(post, two, second, body(second)).result(2), 1)
        finally:
            release.set()
        success(waiting.result(5), 1)
    assert saved(message_db, first)[0].state_version == 1
    assert saved(message_db, second)[0].state_version == 1


@pytest.mark.parametrize("stage", ["route", "pending", "flushed", "commit"])
def test_database_rollback_at_every_write_boundary(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    databases: tuple[Database, Database],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    own = create(browsers[0])
    request = body(own, type="select_mode", mode="knowledge")
    original, _ = saved(message_db, own)
    route = ModeRouter.route

    def failed_route(self: ModeRouter, *args: Any, **kwargs: Any) -> RouterResult:
        route(self, *args, **kwargs)
        raise SQLAlchemyError("SYNTHETIC_AFTER_ROUTING")

    def pending(session: Session, *args: object) -> None:
        if any(isinstance(row, ChatMessage) for row in session.new):
            session.info["synthetic033"] = True
            if stage == "pending":
                session.execute(text("SELECT 1 / 0"))

    def flushed(session: Session, *args: object) -> None:
        if session.info.get("synthetic033"):
            session.execute(text("SELECT 1 / 0"))

    if stage == "route":
        monkeypatch.setattr(ModeRouter, "route", failed_route)
    if stage in {"pending", "flushed"}:
        event.listen(Session, "before_flush", pending)
    if stage == "flushed":
        event.listen(Session, "after_flush_postexec", flushed)
    if stage == "commit":
        # A real PostgreSQL deferred trigger accepts INSERT/flush then rejects
        # COMMIT itself. This is not a mocked Session.commit or a preflush error.
        with databases[1].engine.begin() as connection:
            connection.execute(
                text("""
                CREATE FUNCTION app.synthetic033_commit_failure() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN
                    RAISE EXCEPTION 'SYNTHETIC_COMMIT_REJECTED';
                END $$
            """)
            )
            connection.execute(
                text("""
                CREATE CONSTRAINT TRIGGER synthetic033_commit_failure
                AFTER INSERT ON app.chat_messages DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION app.synthetic033_commit_failure()
            """)
            )
    try:
        error(post(browsers[0], own, request), 503, "SERVICE_UNAVAILABLE")
    finally:
        if stage in {"pending", "flushed"}:
            event.remove(Session, "before_flush", pending)
        if stage == "flushed":
            event.remove(Session, "after_flush_postexec", flushed)
        if stage == "commit":
            with databases[1].engine.begin() as connection:
                connection.execute(
                    text(
                        "DROP TRIGGER synthetic033_commit_failure ON app.chat_messages"
                    )
                )
                connection.execute(
                    text("DROP FUNCTION app.synthetic033_commit_failure()")
                )
    row, records = saved(message_db, own)
    assert records == []
    assert (row.state_version, row.mode, row.last_activity_at, row.expires_at) == (
        0,
        original.mode,
        original.last_activity_at,
        original.expires_at,
    )
    monkeypatch.setattr(ModeRouter, "route", route)
    success(post(browsers[0], own, request), 1)


def test_ownership_admin_cookie_and_request_scope(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
) -> None:
    one, two = browsers
    first, second = create(one), create(two)
    request = body(first)
    for headers, request_body in (
        ({}, request),
        ({"X-Chat-Token": second["session_token"]}, request),
        ({"X-Chat-Token": secrets.token_hex(32)}, request),
        ({"X-Chat-Token": "bad"}, request),
        ({"X-Chat-Token": "A" * 64}, request),
        ([("X-Chat-Token", first["session_token"])] * 2, request),
        (
            {"X-Chat-Token": first["session_token"]},
            request | {"session_id": str(uuid4())},
        ),
    ):
        error(two.post(URL, json=request_body, headers=headers), 404, "NOT_FOUND")
    password = "synthetic033-admin-password"
    with message_db.unit_of_work() as session:
        assert bootstrap_admin(
            session, login="synthetic033-admin", password=password
        ).ok
    csrf = two.get("/admin/auth/csrf").json()["csrf_token"]
    assert (
        two.post(
            "/admin/auth/login",
            json={
                "login": "synthetic033-admin",
                "password": password,
            },
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        ).status_code
        == 200
    )
    assert SESSION_COOKIE in two.cookies
    assert two.get("/api/v1/admin/orders").status_code == 200
    error(two.post(URL, json=request), 404, "NOT_FOUND")
    original = success(post(one, first, request), 1)
    assert success(post(two, first, request), 1) == original
    # The same request UUID can be used independently by another session.
    success(post(two, second, request | {"session_id": second["session_id"]}), 1)
    assert saved(message_db, first)[0].state_version == 1
    assert saved(message_db, second)[0].state_version == 1


@pytest.mark.parametrize("delta,status", [(-1, 200), (0, 410), (1, 410)])
@pytest.mark.parametrize("replay", [False, True])
def test_expiry_boundary_applies_to_new_and_replay(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    delta: int,
    status: int,
    replay: bool,
) -> None:
    own = create(browsers[0])
    request = body(own)
    if replay:
        success(post(browsers[0], own, request), 1)
    expiry = saved(message_db, own)[0].expires_at
    now = expiry + timedelta(microseconds=delta)
    monkeypatch.setattr("app.modules.chat.access.utc_now", lambda: now)
    monkeypatch.setattr("app.modules.chat.messages.utc_now", lambda: now)
    response = post(browsers[0], own, request)
    if status == 200:
        success(response, 1)
    else:
        error(response, 410, "SESSION_EXPIRED")
    error(browsers[1].post(URL, json=request), 404, "NOT_FOUND")
    row, records = saved(message_db, own)
    assert row.state_version == len(records) == (1 if replay or status == 200 else 0)
    assert row.expires_at == expiry


@pytest.mark.parametrize("change", ["expiry", "owner"])
def test_owner_and_expiry_rechecked_after_waiting_for_lock(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    change: str,
) -> None:
    own = create(browsers[0])
    with ThreadPoolExecutor(max_workers=1) as pool:
        with message_db.unit_of_work() as session:
            row = session.scalar(
                select(ChatSession)
                .where(ChatSession.id == UUID(own["session_id"]))
                .with_for_update()
            )
            assert row
            pending = pool.submit(post, browsers[0], own, body(own))
            wait_for_lock(message_db)
            if change == "owner":
                row.owner_token_hash = owner_token_hash(secrets.token_hex(32))
            else:
                row.created_at = utc_now() - timedelta(hours=25)
                row.last_activity_at = row.created_at
                row.expires_at = row.created_at + timedelta(hours=24)
        response = pending.result(5)
    error(
        response,
        410 if change == "expiry" else 404,
        "SESSION_EXPIRED" if change == "expiry" else "NOT_FOUND",
    )
    assert saved(message_db, own)[1] == []


def scenario(
    database: Database,
    tmp_path: Path,
    *,
    questions: int = 1,
) -> tuple[Corpus, UUID, str]:
    with database.unit_of_work() as session:
        if questions == 1:
            c, identifier, path, proof = prepared(session, tmp_path)
        else:
            c = corpus(session)
            value = payload(session, c)
            value["start"] = "q0"
            value["nodes"] = {
                "result": value["nodes"]["result"],
                "handoff": value["nodes"]["handoff"],
            }
            value["nodes"]["result"]["recommendations"] = []
            for index in range(questions):
                value["nodes"][f"q{index}"] = {
                    "type": "question",
                    "text": "SYNTHETIC 033 observation",
                    "risk": "observation",
                    "source_refs": [str(c.section)],
                    "options": [
                        {
                            "id": "a",
                            "text": "SYNTHETIC continue",
                            "next": f"q{index + 1}"
                            if index + 1 < questions
                            else "result",
                        },
                        {"id": "b", "text": "SYNTHETIC stop", "next": "handoff"},
                    ],
                }
            path = write(tmp_path / "synthetic033-chain.yaml", value)
            report = invoke(session, c, path)
            assert report.ok and report.tree_id
            identifier = report.tree_id
            preview = run(session, c, identifier, "preview")
            proof = tmp_path / "synthetic033-chain-proof.json"
            proof.write_text(
                json.dumps(
                    {
                        "tree_id": str(identifier),
                        "yaml_checksum": preview.technical["yaml_checksum"],
                        "binding_sha256": preview.technical["binding_sha256"],
                        "expert_name": "SYNTHETIC ONLY Expert",
                        "review_reference": "SYNTHETIC:033:REVIEW",
                        "approved_at": utc_now().isoformat(),
                        "decision": "approved",
                        "content_checked": True,
                    }
                )
            )
            proof.chmod(0o600)
        assert run(session, c, identifier, "publish", path, proof).ok
        tree = session.get(DiagnosticTree, identifier)
        assert tree
        return c, identifier, tree.key


def start(own: dict[str, Any], c: Corpus, key: str, version: int = 0) -> dict[str, Any]:
    return body(
        own, version, type="start_diagnosis", model_id=str(c.model), tree_key=key
    )


def test_result_feedback_restart_switch_and_rejected_inputs(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    tmp_path: Path,
) -> None:
    one, _ = browsers
    c, pin, key = scenario(message_db, tmp_path)
    own = create(one)
    version = 1
    success(post(one, own, start(own, c, key)), version)
    for input in (
        {"type": "text", "value": "a"},
        {"type": "button", "option_id": "result"},
        {"type": "feedback", "useful": True},
        {"type": "select_mode", "mode": "diagnose"},
    ):
        version += 1
        success(post(one, own, body(own, version - 1, **input)), version)
        row, records = saved(message_db, own)
        assert row.tree_id == pin and row.current_node_key == "observe"
        assert row.outcome is None
        assert decode_response(records[-1]).state_after.transitions == 0
    for forged in ("next", "node_id"):
        error(
            post(
                one,
                own,
                body(
                    own,
                    version,
                    type="button",
                    option_id="a",
                    **{
                        forged: "result",
                    },
                ),
            ),
            422,
            "VALIDATION_ERROR",
        )
    version += 1
    success(
        post(one, own, body(own, version - 1, type="button", option_id="a")), version
    )
    row, records = saved(message_db, own)
    assert row.current_node_key == "result" and row.outcome == "unresolved"
    assert decode_response(records[-1]).state_after.transitions == 1
    version += 1
    success(
        post(one, own, body(own, version - 1, type="feedback", useful=True)), version
    )
    row, records = saved(message_db, own)
    assert row.outcome == "resolved"
    assert decode_response(records[-1]).state_after.transitions == 1
    version += 1
    success(post(one, own, start(own, c, key, version - 1)), version)
    row, records = saved(message_db, own)
    assert row.current_node_key == "observe" and row.outcome is None
    assert decode_response(records[-1]).state_after.transitions == 0
    version += 1
    success(
        post(one, own, body(own, version - 1, type="button", option_id="a")), version
    )
    version += 1
    success(
        post(one, own, body(own, version - 1, type="feedback", useful=False)), version
    )
    row, records = saved(message_db, own)
    assert (
        row.outcome == "handoff"
        and decode_response(records[-1]).state_after.transitions == 1
    )
    version += 1
    success(
        post(one, own, body(own, version - 1, type="select_mode", mode="knowledge")),
        version,
    )
    row, records = saved(message_db, own)
    assert row.mode == "knowledge"
    assert row.tree_id is None and row.current_node_key is None and row.outcome is None
    assert decode_response(records[-1]).state_after.transitions == 0


def test_transition_limit_survives_transactions_new_app_and_replay(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    tmp_path: Path,
) -> None:
    one, _ = browsers
    # Publication itself forbids paths longer than 30. The longest publishable
    # scenario reaches a terminal result on its thirtieth accepted edge.
    c, pin, key = scenario(message_db, tmp_path, questions=30)
    own = create(one)
    success(post(one, own, start(own, c, key)), 1)
    request: dict[str, Any] = {}
    for count in range(1, 31):
        # New HTTP/UoW/ModeRouter every time. Text makes version != transitions.
        version = saved(message_db, own)[0].state_version
        if count == 15:
            success(post(one, own, body(own, version)), version + 1)
            version += 1
        request = body(own, version, type="button", option_id="a")
        response = success(post(one, own, request), version + 1)
        row, records = saved(message_db, own)
        assert row.tree_id == pin
        assert row.current_node_key == (f"q{count}" if count < 30 else "result")
        assert decode_response(records[-1]).state_after.transitions == count
    assert row.outcome == "unresolved" and row.state_version == 32
    # Fresh FastAPI instance and Database pool rule out any in-process counter.
    from app.core.db import DatabaseSettings

    fresh_database = Database(DatabaseSettings.from_environment())
    try:
        fresh_app = FastAPI(lifespan=database_lifespan)
        install_api_errors(fresh_app)
        fresh_app.include_router(chat_router)
        with TestClient(fresh_app) as fresh:
            fresh_app.state.database = fresh_database
            assert success(post(fresh, own, request), 32) == response
            success(post(fresh, own, body(own, 32, type="button", option_id="a")), 33)
    finally:
        fresh_database.close()
    row, records = saved(message_db, own)
    assert row.state_version == len(records) == 33
    assert row.current_node_key == "result" and row.outcome == "unresolved"
    assert decode_response(records[-1]).state_after.transitions == 30


def test_new_publication_retired_pin_and_restart_select_latest(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    tmp_path: Path,
) -> None:
    c, first, key = scenario(message_db, tmp_path)
    own = create(browsers[0])
    success(post(browsers[0], own, start(own, c, key)), 1)
    with message_db.unit_of_work() as session:
        _, second, path, proof = prepared(session, tmp_path, c=c, key=key, version=2)
        assert run(session, c, second, "publish", path, proof).ok
        assert session.get(DiagnosticTree, first).status == "retired"  # type: ignore[union-attr]
    success(post(browsers[0], own, body(own, 1, type="button", option_id="a")), 2)
    assert saved(message_db, own)[0].tree_id == first
    success(post(browsers[0], own, start(own, c, key, 2)), 3)
    row, records = saved(message_db, own)
    assert row.tree_id == second and row.current_node_key == "observe"
    assert decode_response(records[-1]).state_after.transitions == 0


@pytest.mark.parametrize("revoke", ["tree", "source", "visibility"])
def test_revocation_stops_technical_transition_and_hides_old_reply(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    tmp_path: Path,
    revoke: str,
) -> None:
    c, pin, key = scenario(message_db, tmp_path)
    own = create(browsers[0])
    request = start(own, c, key)
    original = success(post(browsers[0], own, request), 1)
    with message_db.unit_of_work() as session:
        if revoke == "tree":
            assert run(session, c, pin, "revoke").ok
        elif revoke == "source":
            session.execute(
                update(DocumentVersion)
                .where(DocumentVersion.id == c.version)
                .values(status="revoked")
            )
        else:
            session.execute(
                update(Document)
                .where(Document.id == c.document)
                .values(visibility="internal")
            )
    assert success(post(browsers[0], own, request), 1) == original
    success(post(browsers[0], own, body(own, 1, type="button", option_id="a")), 2)
    row, records = saved(message_db, own)
    assert row.outcome == "handoff" and row.current_node_key == "observe"
    assert decode_response(records[-1]).state_after.transitions == 0
    history = browsers[0].get(
        f"/api/v1/chat/sessions/{own['session_id']}",
        headers={
            "X-Chat-Token": own["session_token"],
        },
    )
    assert history.status_code == 200
    for hidden in (
        str(pin),
        str(c.section),
        "SYNTHETIC choice",
        "format_version",
        "state_after",
        "transitions",
    ):
        assert hidden not in history.text


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "format",
        "bool",
        "count",
        "absent_count",
        "mode",
        "pin",
        "node",
        "outcome",
        "version",
        "missing_latest",
        "duplicate_version",
    ],
)
def test_corrupt_committed_progress_never_becomes_zero(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    tmp_path: Path,
    damage: str,
) -> None:
    c, pin, key = scenario(message_db, tmp_path, questions=3)
    own = create(browsers[0])
    success(post(browsers[0], own, start(own, c, key)), 1)
    request = body(own, 1, type="button", option_id="a")
    success(post(browsers[0], own, request), 2)
    with message_db.unit_of_work() as session:
        record = session.scalar(
            select(ChatMessage).where(
                ChatMessage.session_id == UUID(own["session_id"]),
                ChatMessage.state_version_after == 2,
            )
        )
        assert record
        envelope = deepcopy(record.response)
        state = envelope["state_after"]
        assert isinstance(state, dict)
        if damage == "missing":
            envelope = {"SYNTHETIC_PRIVATE_POISON": "never public"}
        elif damage == "format":
            envelope["format_version"] = 2
        elif damage == "absent_count":
            del state["transitions"]
        elif damage == "missing_latest":
            session.delete(record)
        elif damage == "duplicate_version":
            session.add(
                ChatMessage(
                    session_id=record.session_id,
                    request_id=uuid4(),
                    request_hash="a" * 64,
                    input={},
                    response=envelope,
                    state_version_after=2,
                    matched_by="none",
                    latency_ms=0,
                )
            )
        else:
            changes: dict[str, tuple[str, JsonValue]] = {
                "bool": ("transitions", True),
                "count": ("transitions", 31),
                "mode": ("mode", "commerce"),
                "pin": ("tree_id", str(uuid4())),
                "node": ("current_node_key", "q2"),
                "outcome": ("outcome", "resolved"),
                "version": ("state_version", 1),
            }
            field, value = changes[damage]
            state[field] = value
        if damage != "missing_latest":
            # Explicit UPDATE matters: Python considers True == 1, so ORM
            # dirty comparison alone would skip our deliberately corrupt bool.
            session.execute(
                update(ChatMessage)
                .where(ChatMessage.id == record.id)
                .values(response=envelope)
                .execution_options(synchronize_session=False)
            )
    before, messages = saved(message_db, own)
    error(
        post(browsers[0], own, body(own, 2, type="button", option_id="a")),
        503,
        "SERVICE_UNAVAILABLE",
    )
    after, remaining = saved(message_db, own)
    assert after.state_version == 2 and len(remaining) == len(messages)
    assert after.current_node_key == before.current_node_key == "q1"
    assert after.last_activity_at == before.last_activity_at and after.tree_id == pin
    history = browsers[0].get(
        f"/api/v1/chat/sessions/{own['session_id']}",
        headers={"X-Chat-Token": own["session_token"]},
    )
    assert history.status_code == 200
    assert "SYNTHETIC_PRIVATE_POISON" not in history.text
    assert "state_after" not in history.text


def test_masking_hash_original_identity_and_no_leaks(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    own = create(browsers[0], "knowledge")
    caplog.set_level(logging.DEBUG)
    private = "SYNTHETIC 033: synthetic033@example.invalid, +7 (999) 123-45-67"
    request = body(own, type="text", value=private)
    original = success(post(browsers[0], own, request), 1)
    assert success(post(browsers[1], own, request), 1) == original
    row, records = saved(message_db, own)
    assert records[0].input == {
        "type": "text",
        "value": "SYNTHETIC 033: [email], [phone]",
    }
    # Different original PII must conflict even if minimized storage is equal.
    changed = deepcopy(request)
    changed["input"]["value"] = private.replace("synthetic033@", "synthetic034@")
    error(post(browsers[0], own, changed), 409, "IDEMPOTENCY_CONFLICT")
    assert row.state_version == 1
    history = browsers[0].get(
        f"/api/v1/chat/sessions/{own['session_id']}",
        headers={"X-Chat-Token": own["session_token"]},
    )
    with message_db.unit_of_work() as session:
        storage = str(
            session.execute(
                text("SELECT row_to_json(m) FROM app.chat_messages m")
            ).all()
        )
        assert session.scalar(text("SELECT count(*) FROM app.orders")) == 0
        assert session.scalar(text("SELECT count(*) FROM app.email_outbox")) == 0
    output = capsys.readouterr()
    visible = (
        history.text + json.dumps(original) + caplog.text + output.out + output.err
    )
    for secret in (
        own["session_token"],
        private,
        "synthetic033@example.invalid",
        "+7 (999) 123-45-67",
    ):
        assert secret not in visible + storage
    assert "format_version" not in visible and "state_after" not in visible
