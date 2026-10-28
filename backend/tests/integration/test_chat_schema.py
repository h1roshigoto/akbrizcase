"""MVP-029: real constraints, concurrent writers and independent order retention."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_orders_schema import catalog_item, item_values, new_order

from app.core.db import Database
from app.core.db_types import Base
from app.modules.catalog.models import EquipmentModel
from app.modules.chat.models import ChatMessage, ChatSession
from app.modules.kb.tree_models import DiagnosticTree
from app.modules.notify.models import EmailOutbox
from app.modules.orders.models import Order, OrderItem, OrderStatusHistory
from app.modules.orders.read_service import OrderReadService

pytestmark = pytest.mark.integration


@pytest.fixture
def session(database: Database) -> Iterator[Session]:
    with database.sessions() as current:
        try:
            yield current
        finally:
            current.rollback()


def chat(session: Session, **changes: Any) -> ChatSession:
    row = ChatSession(**({"owner_token_hash": "a" * 64, "mode": "diagnose"} | changes))
    session.add(row)
    session.flush()
    return row


def exchange(session_id: UUID, /, **changes: Any) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "request_id": uuid4(),
        "request_hash": "b" * 64,
        "input": {"type": "button", "option_id": "synthetic"},
        "response": {
            "text": "SYNTHETIC ONLY",
            "actions": [{"type": "contact_specialist"}],
            "sources": [{"section_id": str(uuid4())}],
        },
        "state_version_after": 1,
        "matched_by": "button",
        "latency_ms": 0,
    } | changes


@pytest.mark.parametrize("mode", ["diagnose", "knowledge", "commerce"])
def test_roundtrip_defaults_and_atomic_exchange(session: Session, mode: str) -> None:
    row = chat(session, mode=mode)
    assert row.id.version == 4 and row.state_version == 0
    assert row.tree_id is None and row.current_node_key is None and row.outcome is None
    assert row.expires_at - row.created_at == timedelta(hours=24)
    assert row.last_activity_at == row.created_at
    values = exchange(row.id)
    message = session.scalars(
        insert(ChatMessage).values(**values).returning(ChatMessage)
    ).one()
    assert message.id.version == 4 and message.request_id == values["request_id"]
    assert message.input == values["input"] and message.response == values["response"]
    assert message.state_version_after == 1 and message.latency_ms == 0
    for at in (
        row.created_at,
        row.last_activity_at,
        row.expires_at,
        message.created_at,
    ):
        assert at.utcoffset() == timedelta(0)
    # A single exchange row stores both input and response; no partial response.
    assert (
        session.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.session_id == row.id)
        )
        == 1
    )


def test_non_utc_input_is_read_back_in_utc(session: Session) -> None:
    created = datetime(2026, 10, 28, 12, tzinfo=timezone(timedelta(hours=3)))
    row = chat(
        session,
        created_at=created,
        last_activity_at=created,
        expires_at=created + timedelta(hours=24),
    )
    session.expire_all()
    assert row.created_at == datetime(2026, 10, 28, 9, tzinfo=UTC)
    assert row.created_at.utcoffset() == timedelta(0)
    assert session.scalar(text("SHOW timezone")) == "UTC"


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_token_hash": None},
        {"owner_token_hash": "raw-synthetic-token"},
        {"owner_token_hash": "a" * 63},
        {"owner_token_hash": "G" * 64},
        {"mode": "handoff"},
        {"mode": None},
        {"state_version": -1},
        {"state_version": None},
        {"outcome": "success"},
        {"current_node_key": "start"},
        {"tree_id": uuid4()},
    ],
)
def test_session_constraints(session: Session, changes: dict[str, Any]) -> None:
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            insert(Base.metadata.tables["app.chat_sessions"]).values(
                **({"owner_token_hash": "a" * 64, "mode": "diagnose"} | changes)
            )
        )


@pytest.mark.parametrize(
    "column,expression",
    [
        ("created_at", "'infinity'::timestamptz"),
        ("last_activity_at", "'-infinity'::timestamptz"),
        ("last_activity_at", "created_at - interval '1 second'"),
        ("last_activity_at", "expires_at + interval '1 second'"),
        ("expires_at", "'infinity'::timestamptz"),
        ("expires_at", "created_at + interval '25 hours'"),
        ("expires_at", "created_at"),
    ],
)
def test_session_time_constraints(
    session: Session, column: str, expression: str
) -> None:
    row = chat(session)
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text(f"UPDATE app.chat_sessions SET {column}={expression} WHERE id=:id"),
            {"id": row.id},
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"session_id": uuid4()},
        {"session_id": None},
        {"request_id": None},
        {"request_hash": None},
        {"request_hash": "raw-synthetic-input"},
        {"request_hash": "x" * 64},
        {"input": None},
        {"input": []},
        {"input": "text"},
        {"response": None},
        {"response": []},
        {"response": "text"},
        {"state_version_after": 0},
        {"state_version_after": -1},
        {"state_version_after": None},
        {"matched_by": "llm"},
        {"matched_by": None},
        {"latency_ms": -1},
        {"latency_ms": None},
    ],
)
def test_message_constraints(session: Session, changes: dict[str, Any]) -> None:
    row = chat(session)
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(insert(ChatMessage).values(**exchange(row.id, **changes)))


def test_message_finite_time(session: Session) -> None:
    row = chat(session)
    message = ChatMessage(**exchange(row.id))
    session.add(message)
    session.flush()
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text("UPDATE app.chat_messages SET created_at='infinity' WHERE id=:id"),
            {"id": message.id},
        )


@pytest.mark.parametrize("matched_by", ["button", "keyword", "search", "none"])
def test_message_matching_enum(session: Session, matched_by: str) -> None:
    row = chat(session)
    session.execute(
        insert(ChatMessage).values(**exchange(row.id, matched_by=matched_by))
    )


@pytest.mark.parametrize("outcome", ["resolved", "handoff", "unresolved"])
def test_session_outcomes(session: Session, outcome: str) -> None:
    assert chat(session, outcome=outcome).outcome == outcome


def test_exact_tree_version_and_restrict(session: Session) -> None:
    model = EquipmentModel(
        code="SYNTHETIC029-" + uuid4().hex,
        name="SYNTHETIC",
        description="Synthetic only",
    )
    session.add(model)
    session.flush()
    key = "SYNTHETIC029-" + uuid4().hex
    trees = [
        DiagnosticTree(
            key=key,
            version=version,
            model_id=model.id,
            title="SYNTHETIC ONLY",
            graph={},
            yaml_checksum="a" * 64,
            git_commit="b" * 40,
        )
        for version in (1, 2)
    ]
    session.add_all(trees)
    session.flush()
    row = chat(session, tree_id=trees[0].id, current_node_key="start")
    # A later edition with the same logical key never repoints an existing session.
    session.expire_all()
    assert row.tree_id == trees[0].id and row.tree_id != trees[1].id
    for statement in (
        delete(DiagnosticTree).where(DiagnosticTree.id == row.tree_id),
        update(ChatSession).where(ChatSession.id == row.id).values(mode="knowledge"),
        update(ChatSession)
        .where(ChatSession.id == row.id)
        .values(current_node_key=" "),
    ):
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(statement)
    # The FK protects referenced editions, without introducing a tree lifecycle.
    session.execute(delete(DiagnosticTree).where(DiagnosticTree.id == trees[1].id))


def test_request_id_scoped_to_session(session: Session) -> None:
    one, two = chat(session), chat(session)
    request_id = uuid4()
    for row in (one, two):
        session.execute(
            insert(ChatMessage).values(**exchange(row.id, request_id=request_id))
        )
    with pytest.raises(DBAPIError) as caught, session.begin_nested():
        session.execute(
            insert(ChatMessage).values(**exchange(one.id, request_id=request_id))
        )
    assert caught.value.orig.sqlstate == "23505"  # type: ignore[union-attr]


def test_delete_cascades_messages_and_preserves_whole_orders(session: Session) -> None:
    row = chat(session)
    other = chat(session)
    session.add_all(
        [ChatMessage(**exchange(row.id)), ChatMessage(**exchange(other.id))]
    )
    support = new_order(
        session,
        kind="support",
        source="chat",
        chat_session_id=row.id,
        comment="SYNTHETIC independent problem description",
        known_subtotal=0,
        items_total=None,
        total_amount=None,
    )
    commercial = new_order(session, source="chat", chat_session_id=row.id)
    site = new_order(session)
    item_id = catalog_item(session)
    session.execute(insert(OrderItem).values(**item_values(commercial.id, item_id)))
    for order in (support, commercial, site):
        session.add(EmailOutbox(order_id=order.id))
        session.add(OrderStatusHistory(order_id=order.id, to_status="new"))
    session.flush()
    tables = (
        "orders",
        "order_items",
        "order_status_history",
        "consents",
        "consent_texts",
        "email_outbox",
        "catalog_items",
    )

    def snapshot() -> dict[str, list[Any]]:
        return {
            table: [
                dict(row)
                for row in session.execute(
                    text(f"SELECT * FROM app.{table} ORDER BY 1")
                ).mappings()
            ]
            for table in tables
        }

    before = snapshot()
    session.execute(delete(ChatSession).where(ChatSession.id == row.id))
    after = snapshot()
    expected_orders = [
        dict(record) | {"chat_session_id": None}
        if record["chat_session_id"] == row.id
        else dict(record)
        for record in before["orders"]
    ]
    assert after["orders"] == expected_orders
    assert {k: v for k, v in after.items() if k != "orders"} == {
        k: v for k, v in before.items() if k != "orders"
    }
    assert (
        session.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.session_id == row.id)
        )
        == 0
    )
    assert (
        session.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.session_id == other.id)
        )
        == 1
    )
    session.expire_all()
    assert support.chat_session_id is None and commercial.chat_session_id is None
    assert OrderReadService(session).detail(support.id).comment == support.comment
    # No exemption from the existing mandatory description after chat deletion.
    for comment in (None, "", " ", "x" * 2001):
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(
                update(Order).where(Order.id == support.id).values(comment=comment)
            )
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            update(Order).where(Order.id == site.id).values(chat_session_id=uuid4())
        )
    # The existing PII erasure exception still works independently of chat.
    session.execute(
        update(Order)
        .where(Order.id == support.id)
        .values(
            comment=None,
            contact_name=None,
            email=None,
            request_hash=None,
            receipt_token_hash=None,
            pii_erased_at=func.now(),
        )
    )


def test_exchange_and_state_rollback_together(database: Database) -> None:
    with database.unit_of_work() as session:
        identifier = chat(session).id
    try:
        with pytest.raises(DBAPIError):
            with database.unit_of_work() as session:
                session.execute(
                    update(ChatSession)
                    .where(ChatSession.id == identifier)
                    .values(state_version=1)
                )
                session.execute(insert(ChatMessage).values(**exchange(identifier)))
                session.execute(
                    insert(ChatMessage).values(**exchange(identifier, response=None))
                )
        with database.unit_of_work() as session:
            row = session.get(ChatSession, identifier)
            assert row and row.state_version == 0
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ChatMessage)
                    .where(ChatMessage.session_id == identifier)
                )
                == 0
            )
    finally:
        with database.unit_of_work() as session:
            session.execute(delete(ChatSession).where(ChatSession.id == identifier))


@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_inserts(database: Database, same_request: bool) -> None:
    with database.unit_of_work() as session:
        identifier = chat(session).id
    request_id = uuid4()
    barrier = Barrier(2)

    def write(index: int) -> str:
        try:
            with database.unit_of_work() as session:
                barrier.wait(timeout=10)
                session.execute(
                    insert(ChatMessage).values(
                        **exchange(
                            identifier,
                            request_id=request_id if same_request else uuid4(),
                            state_version_after=index + 1,
                        )
                    )
                )
            return "inserted"
        except DBAPIError as error:
            assert error.orig.sqlstate == "23505"  # type: ignore[union-attr]
            return "duplicate"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, (0, 1)))
        assert sorted(results) == (
            ["duplicate", "inserted"] if same_request else ["inserted", "inserted"]
        )
        with database.unit_of_work() as session:
            assert session.scalar(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.session_id == identifier)
            ) == (1 if same_request else 2)
    finally:
        with database.unit_of_work() as session:
            session.execute(delete(ChatSession).where(ChatSession.id == identifier))


def test_indexes_and_jsonb_types(session: Session) -> None:
    indexes = {
        str(row[0]): str(row[1])
        for row in session.execute(
            text(
                "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='app' "
                "AND tablename IN ('chat_sessions','chat_messages','orders')"
            )
        )
    }
    for name, fragment in (
        ("uq_chat_messages_session_id", "UNIQUE INDEX"),
        ("ix_chat_messages_session_id_created_at", "(session_id, created_at)"),
        ("ix_chat_sessions_expires_at", "(expires_at)"),
        ("ix_orders_chat_session_id", "(chat_session_id)"),
    ):
        assert fragment in indexes[name]
    assert "(session_id, request_id)" in indexes["uq_chat_messages_session_id"]
    assert {
        str(row[0]): str(row[1])
        for row in session.execute(
            text(
                "SELECT column_name,data_type FROM information_schema.columns "
                "WHERE table_schema='app' AND table_name='chat_messages' "
                "AND column_name IN ('input','response')"
            )
        )
    } == {"input": "jsonb", "response": "jsonb"}
