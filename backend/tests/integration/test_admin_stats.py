"""MVP-051: real PG16 aggregates, actual staff HTTP, feedback and deletion."""

import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from test_kb_import import session as session
from test_order_reads import PASSWORD, create, login
from test_order_reads import read_client as read_client
from test_tree_lifecycle import prepared, run

from app.admin.stats import read_statistics
from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff_models import StaffSession, StaffUser
from app.modules.chat.messages import create_message
from app.modules.chat.models import ChatMessage, ChatSession
from app.modules.chat.service import create_session
from app.modules.kb.tree_models import DiagnosticTree
from app.modules.orders.cleanup_service import apply_command, eligibility, prepare_entry
from app.modules.orders.deletion_journal import SyntheticJournal, command_for
from app.modules.orders.deletion_operator import prepare_deletion
from app.modules.orders.models import Order
from app.modules.orders.seed import DEMO_VERSION
from app.schemas.admin_stats import FeedbackStats, StatsPeriod
from app.schemas.chat import ChatMessageCreate, ChatSessionCreate, ChatSessionCreated

pytestmark = pytest.mark.integration
PERIOD = StatsPeriod.from_query([("from", "2026-04-01"), ("to", "2026-04-02")])
URL = "/admin/stats?from=2026-04-01&to=2026-04-02"
CANARIES = (
    "stats-private@example.invalid",
    "+7 999 123 45 67",
    "SYNTHETIC-PRIVATE-COMMENT",
    "SYNTHETIC-PRIVATE-ORG",
    "SYNTHETIC-PRIVATE-TOKEN",
    "SYNTHETIC-PRIVATE-QUESTION",
)


def counters(client: TestClient, url: str = URL) -> dict[str, int]:
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    return {
        key: int(value)
        for key, value in re.findall(
            r'data-counter="([a-z_.]+)">(\d+)</td>', response.text
        )
    }


def message(
    session: Session,
    owner: UUID,
    at: datetime,
    useful: Any = True,
    *,
    matched: str = "button",
    resolution: str = "completed",
    outcome: str = "resolved",
    input_type: str = "feedback",
    mode: str = "diagnose",
    format_version: Any = 1,
) -> None:
    session.add(
        ChatMessage(
            session_id=owner,
            request_id=uuid4(),
            request_hash="a" * 64,
            input={"type": input_type, "useful": useful, "value": CANARIES[5]},
            response={
                "format_version": format_version,
                "reply": {"mode": mode, "resolution": resolution, "text": CANARIES[4]},
                "state_after": {"mode": mode, "outcome": outcome},
            },
            state_version_after=1,
            matched_by=matched,
            latency_ms=0,
            created_at=at,
        )
    )


@pytest.fixture
def dataset(
    read_client: TestClient, databases: tuple[Database, Database]
) -> Iterator[tuple[list[UUID], list[UUID]]]:
    database = databases[0]
    orders = [
        create(read_client, database, kind)
        for kind in ("commercial", "support", "commercial", "support")
    ]
    chats: list[UUID] = []
    with database.unit_of_work() as session:
        for index, (mode, outcome) in enumerate(
            (
                ("diagnose", "resolved"),
                ("diagnose", "handoff"),
                ("knowledge", "unresolved"),
                ("commerce", None),
                ("diagnose", None),
                ("knowledge", None),
            )
        ):
            at = (
                PERIOD.start_utc
                if index < 4
                else PERIOD.start_utc - timedelta(days=1)
                if index == 4
                else PERIOD.stop_utc
            )
            row = ChatSession(
                owner_token_hash="b" * 64,
                retention_version=DEMO_VERSION,
                mode=mode,
                outcome=outcome,
                created_at=at,
                last_activity_at=at,
                expires_at=at + timedelta(hours=24),
            )
            session.add(row)
            session.flush()
            chats.append(row.id)
        for identifier, status, source in zip(
            orders,
            ("new", "in_progress", "closed", "cancelled"),
            ("site", "chat", "site", "chat"),
            strict=True,
        ):
            session.execute(
                update(Order)
                .where(Order.id == identifier)
                .values(
                    created_at=PERIOD.start_utc,
                    status=status,
                    source=source,
                    chat_session_id=chats[1] if source == "chat" else None,
                    email=CANARIES[0],
                    phone=CANARIES[1],
                    comment=CANARIES[2],
                    organization_name=CANARIES[3],
                )
            )
        # Feedback uses its own created_at, including an older session.
        message(session, chats[4], PERIOD.start_utc)
        message(
            session,
            chats[1],
            PERIOD.stop_utc - timedelta(microseconds=1),
            False,
            resolution="handoff",
            outcome="handoff",
        )
        message(session, chats[0], PERIOD.start_utc - timedelta(microseconds=1))
        message(session, chats[0], PERIOD.stop_utc)
        # Rejected feedback can retain a previous terminal result.
        message(session, chats[0], PERIOD.start_utc, matched="none")
        message(
            session,
            chats[1],
            PERIOD.start_utc,
            False,
            matched="none",
            resolution="handoff",
            outcome="handoff",
        )
        message(
            session,
            chats[0],
            PERIOD.start_utc,
            resolution="question",
            outcome="unresolved",
        )
        message(
            session,
            chats[0],
            PERIOD.start_utc,
            resolution="no_match",
            outcome="handoff",
        )
        message(session, chats[0], PERIOD.start_utc, input_type="button")
        message(session, chats[0], PERIOD.start_utc, "true")
        message(session, chats[0], PERIOD.start_utc, None)
        message(session, chats[0], PERIOD.start_utc, format_version="1")
        message(session, chats[0], PERIOD.start_utc, mode="knowledge")
    try:
        yield orders, chats
    finally:
        with database.unit_of_work() as session:
            session.execute(delete(ChatSession).where(ChatSession.id.in_(chats)))


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_exact_aggregates_html_privacy_and_no_business_writes(
    read_client: TestClient,
    databases: tuple[Database, Database],
    dataset: tuple[list[UUID], list[UUID]],
    role: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="akbrizcase.runtime")
    login(read_client, role)
    database = databases[0]
    tables = ("orders", "chat_sessions", "chat_messages")

    def snapshot() -> dict[str, list[Any]]:
        with database.unit_of_work() as session:
            return {
                table: list(
                    session.execute(
                        text(f"SELECT row_to_json(t) FROM app.{table} t ORDER BY id")
                    ).scalars()
                )
                for table in tables
            }

    before = snapshot()
    values = counters(read_client)
    assert values == {
        "orders.total": 4,
        "orders.commercial": 2,
        "orders.support": 2,
        "orders.new": 1,
        "orders.in_progress": 1,
        "orders.closed": 1,
        "orders.cancelled": 1,
        "orders.site": 2,
        "orders.chat": 2,
        "orders.chat_linked": 2,
        "sessions.total": 4,
        "sessions.diagnose": 2,
        "sessions.knowledge": 1,
        "sessions.commerce": 1,
        "sessions.resolved": 1,
        "sessions.handoff": 1,
        "sessions.unresolved": 1,
        "sessions.no_outcome": 1,
        "feedback.positive": 1,
        "feedback.negative": 1,
    }
    assert snapshot() == before
    response = read_client.get(URL)
    for secret in (
        *CANARIES,
        *(str(identifier) for ids in dataset for identifier in ids),
    ):
        assert secret not in response.text
        assert secret not in caplog.text
    assert re.search(
        r'href="(?:https://localhost:8443)?/admin/stats"',
        read_client.get("/admin/").text,
    )
    # Direct URL authorization and ordinary status service share the same UoW.
    csrf = login(read_client, role)
    changed = read_client.patch(
        f"/api/v1/admin/orders/{dataset[0][0]}/status",
        json={"status": "in_progress", "expected_version": 1},
        headers={"Origin": "https://localhost:8443", "X-CSRF-Token": csrf},
    )
    assert changed.status_code == 200
    after = counters(read_client)
    assert after == values | {"orders.new": 0, "orders.in_progress": 2}


def test_empty_period_and_each_exact_utc_boundary(
    read_client: TestClient,
    databases: tuple[Database, Database],
    dataset: tuple[list[UUID], list[UUID]],
) -> None:
    login(read_client)
    assert all(
        value == 0
        for value in counters(
            read_client, "/admin/stats?from=2000-01-01&to=2000-01-01"
        ).values()
    )
    database = databases[0]
    orders, chats = dataset
    with database.unit_of_work() as session:
        session.execute(
            update(Order)
            .where(Order.id.in_(orders))
            .values(created_at=PERIOD.start_utc - timedelta(days=2))
        )
        session.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(chats)))
        for identifier in chats:
            at = PERIOD.start_utc - timedelta(days=2)
            session.execute(
                update(ChatSession)
                .where(ChatSession.id == identifier)
                .values(
                    created_at=at,
                    last_activity_at=at,
                    expires_at=at + timedelta(hours=24),
                )
            )
        message(session, chats[0], PERIOD.start_utc)
    for at, expected in (
        (PERIOD.start_utc - timedelta(microseconds=1), 0),
        (PERIOD.start_utc, 1),
        (PERIOD.stop_utc - timedelta(microseconds=1), 1),
        (PERIOD.stop_utc, 0),
    ):
        with database.unit_of_work() as session:
            session.execute(
                update(Order).where(Order.id == orders[0]).values(created_at=at)
            )
            session.execute(
                update(ChatSession)
                .where(ChatSession.id == chats[0])
                .values(
                    created_at=at,
                    last_activity_at=at,
                    expires_at=at + timedelta(hours=24),
                )
            )
            session.execute(
                update(ChatMessage)
                .where(ChatMessage.session_id == chats[0])
                .values(created_at=at)
            )
        values = counters(read_client)
        assert (
            values["orders.total"],
            values["sessions.total"],
            values["feedback.positive"],
        ) == (expected,) * 3


@pytest.mark.parametrize("state", ["anonymous", "disabled", "revoked"])
def test_staff_denial_for_direct_stats_url(
    read_client: TestClient, databases: tuple[Database, Database], state: str
) -> None:
    if state != "anonymous":
        login(read_client)
        with databases[0].unit_of_work() as session:
            if state == "disabled":
                session.execute(
                    update(StaffUser)
                    .where(StaffUser.login == "read-manager")
                    .values(is_active=False)
                )
            else:
                session.execute(update(StaffSession).values(revoked_at=utc_now()))
    response = read_client.get(URL)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "query",
    [
        "from=2026-04-01",
        "from=2026-04-01&to=2026-04-02&from=2026-04-01",
        "from=2026-04-01&to=2026-04-02&email=stats-private%40example.invalid",
        "from=2026-02-29&to=2026-03-01",
        "from=2026-04-02&to=2026-04-01",
        "from=2026-01-01&to=2026-04-01",
    ],
)
def test_invalid_query_is_422_and_private(
    read_client: TestClient, query: str, caplog: pytest.LogCaptureFixture
) -> None:
    login(read_client)
    response = read_client.get("/admin/stats?" + query)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert CANARIES[0] not in response.text + caplog.text


def test_real_postgresql_error_fails_closed(
    read_client: TestClient,
    databases: tuple[Database, Database],
    caplog: pytest.LogCaptureFixture,
) -> None:
    login(read_client)

    def fail(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        many: bool,
    ) -> tuple[str, Any]:
        if "order_statistics" in statement:
            return "SELECT 1 / 0", ()
        return statement, parameters

    event.listen(databases[0].engine, "before_cursor_execute", fail, retval=True)
    try:
        response = read_client.get(URL)
    finally:
        event.remove(databases[0].engine, "before_cursor_execute", fail)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert "SELECT" not in response.text
    assert "division by zero" not in response.text + caplog.text
    assert all(value == 0 for value in counters(read_client).values())


def test_single_snapshot_survives_concurrent_commit(
    read_client: TestClient,
    databases: tuple[Database, Database],
    dataset: tuple[list[UUID], list[UUID]],
) -> None:
    database = databases[0]
    calls = 0

    def committed_after_statement(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        many: bool,
    ) -> None:
        nonlocal calls
        if "order_statistics" not in statement:
            return
        calls += 1
        with database.unit_of_work() as writer:
            writer.execute(
                update(Order)
                .where(Order.id == dataset[0][0])
                .values(status="in_progress")
            )
            writer.execute(
                update(ChatSession)
                .where(ChatSession.id == dataset[1][0])
                .values(outcome="handoff")
            )

    event.listen(database.engine, "after_cursor_execute", committed_after_statement)
    try:
        with database.unit_of_work() as session:
            stats = read_statistics(session, PERIOD)
            assert not session.new and not session.dirty and not session.deleted
    finally:
        event.remove(database.engine, "after_cursor_execute", committed_after_statement)
    assert calls == 1
    assert stats.orders.new == stats.sessions.resolved == 1
    with database.unit_of_work() as session:
        current = read_statistics(session, PERIOD)
    assert current.orders.new == current.sessions.resolved == 0
    assert current.orders.total == stats.orders.total == 4


def test_actual_router_feedback_and_replay(session: Session, tmp_path: Path) -> None:
    c, tree, path, proof = prepared(session, tmp_path)
    assert run(session, c, tree, "publish", path, proof).ok
    period = StatsPeriod.from_query([])
    before = read_statistics(session, period).feedback
    for useful in (True, False):
        own = create_session(session, ChatSessionCreate(mode="diagnose"))
        version = 0

        def send(
            input: dict[str, Any], own: ChatSessionCreated = own
        ) -> tuple[UUID, ChatMessageCreate]:
            nonlocal version
            body = ChatMessageCreate.model_validate(
                {
                    "session_id": own.session_id,
                    "request_id": uuid4(),
                    "expected_version": version,
                    "input": input,
                }
            )
            identifier = create_message(session, body, own.session_token)
            version += 1
            return identifier, body

        published = session.get(DiagnosticTree, tree)
        assert published
        send(
            {
                "type": "start_diagnosis",
                "model_id": str(c.model),
                "tree_key": published.key,
            }
        )
        # A premature positive feedback is persisted but rejected.
        send({"type": "feedback", "useful": True})
        send({"type": "button", "option_id": "a"})
        assert read_statistics(session, period).feedback == before
        accepted, body = send({"type": "feedback", "useful": useful})
        assert create_message(session, body, own.session_token) == accepted
        # Feedback after a terminal result cannot be accepted again.
        send({"type": "feedback", "useful": useful})
        expected = FeedbackStats(
            positive=before.positive + int(useful),
            negative=before.negative + int(not useful),
        )
        assert read_statistics(session, period).feedback == expected
        before = expected


def test_authorized_retention_and_order_deletion_reduce_counts(
    read_client: TestClient,
    databases: tuple[Database, Database],
    tmp_path: Path,
) -> None:
    runtime, migrator = databases
    order_id = create(read_client, runtime, "support")
    with runtime.unit_of_work() as session:
        own = create_session(session, ChatSessionCreate(mode="diagnose"))
        chat = session.get(ChatSession, own.session_id)
        assert chat is not None and chat.retention_version == DEMO_VERSION
        due_at = chat.created_at + timedelta(days=30)
        message(
            session,
            chat.id,
            chat.created_at,
            False,
            resolution="handoff",
            outcome="handoff",
        )
        session.execute(
            update(Order)
            .where(Order.id == order_id)
            .values(source="chat", chat_session_id=chat.id)
        )
    login(read_client)
    before = counters(read_client, "/admin/stats")
    with runtime.unit_of_work() as session:
        chat = session.get(ChatSession, own.session_id)
        assert chat
        bundle, due, review = eligibility(session, "chat_delete", chat, due_at)
        assert due and not review
        command = command_for(
            prepare_entry(session, "chat_delete", chat.id, bundle, due_at)
        )
    directory = tmp_path / "chat-journal"
    directory.mkdir(mode=0o700)
    proof = SyntheticJournal(directory).witness(command, synthetic=True)
    with runtime.unit_of_work() as session:
        assert apply_command(session, command, proof, now=due_at)
    after_chat = counters(read_client, "/admin/stats")
    assert after_chat == before | {
        "sessions.total": before["sessions.total"] - 1,
        "sessions.diagnose": before["sessions.diagnose"] - 1,
        "sessions.no_outcome": before["sessions.no_outcome"] - 1,
        "feedback.negative": before["feedback.negative"] - 1,
        "orders.chat_linked": before["orders.chat_linked"] - 1,
    }
    # Use the existing protected operator and witnessed deletion journal.
    with migrator.unit_of_work() as session:
        code, order_command = prepare_deletion(
            session,
            login="read-admin",
            password=PASSWORD,
            order_id=order_id,
            expected_version=1,
            decision_id=uuid4(),
            journal_policy_version=DEMO_VERSION,
        )
        assert code == "PREPARED" and order_command is not None
    directory = tmp_path / "order-journal"
    directory.mkdir(mode=0o700)
    proof = SyntheticJournal(directory).witness(order_command, synthetic=True)
    with migrator.unit_of_work() as session:
        assert apply_command(session, order_command, proof, manual_authorized=True)
    assert counters(read_client, "/admin/stats") == after_chat | {
        "orders.total": after_chat["orders.total"] - 1,
        "orders.support": after_chat["orders.support"] - 1,
        "orders.new": after_chat["orders.new"] - 1,
        "orders.chat": after_chat["orders.chat"] - 1,
    }


def test_read_does_not_flush_pending_orm_changes(
    read_client: TestClient,
    databases: tuple[Database, Database],
    dataset: tuple[list[UUID], list[UUID]],
) -> None:
    database = databases[0]
    with database.sessions() as session:
        order = session.get(Order, dataset[0][0])
        chat = session.get(ChatSession, dataset[1][0])
        assert order and chat
        order.status = "in_progress"
        chat.outcome = "handoff"
        dirty = set(session.dirty)
        stats = read_statistics(session, PERIOD)
        assert stats.orders.new == stats.sessions.resolved == 1
        assert set(session.dirty) == dirty
        assert order.status == "in_progress" and chat.outcome == "handoff"
        session.rollback()
    with database.unit_of_work() as session:
        stats = read_statistics(session, PERIOD)
        assert stats.orders.new == stats.sessions.resolved == 1
