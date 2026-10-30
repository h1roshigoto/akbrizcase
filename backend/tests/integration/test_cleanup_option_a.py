"""Option A: protected graph deletion, independent evidence and chat lifecycle."""

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Row, event, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_cleanup import create, terminal, witness
from test_cleanup import isolated_cleanup as isolated_cleanup
from test_privacy import PASSWORD
from test_privacy import client as client
from test_privacy import privacy_db as privacy_db

from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff_models import AuditEvent
from app.modules.chat.models import ChatMessage, ChatSession
from app.modules.notify.models import EmailOutbox
from app.modules.orders.cleanup_service import (
    apply_command,
    prepare_batch,
    prepare_entry,
)
from app.modules.orders.deletion_journal import JournalUnavailable, command_for
from app.modules.orders.deletion_models import DeletionEntry
from app.modules.orders.deletion_operator import prepare_deletion
from app.modules.orders.erasure_service import PII_FIELDS
from app.modules.orders.models import Consent, ConsentText, Order, OrderItem
from app.modules.orders.privacy_models import ConsentPublication
from app.modules.orders.seed import DEMO_VERSION, demo_bundle

pytestmark = pytest.mark.integration

GRAPH_TABLES = (
    "orders",
    "order_items",
    "order_status_history",
    "email_outbox",
    "consents",
)


def graph(session: Session) -> dict[str, list[Row[Any]]]:
    # Full synthetic rows catch partial rollback, lost reasons and changed snapshots.
    return {
        table: list(session.execute(text(f"SELECT * FROM app.{table} ORDER BY id")))
        for table in GRAPH_TABLES
    }


@pytest.mark.parametrize("kind", ["support", "commercial", "mixed", "service"])
@pytest.mark.parametrize("erased", [False, True])
@pytest.mark.parametrize("failure", ["before_commit", "during_commit", "after_commit"])
def test_protected_full_graph_delete_rollback_retry_and_independent_chat(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    kind: str,
    erased: bool,
    failure: str,
) -> None:
    runtime, migrator = privacy_db
    identifier = create(client, runtime, kind)
    sibling = create(client, runtime, "support")
    response = client.post("/api/v1/chat/sessions", json={"mode": "knowledge"})
    assert response.status_code == 201
    chat_id = UUID(response.json()["session_id"])
    with runtime.unit_of_work() as session:
        row = session.get(Order, identifier)
        other = session.get(Order, sibling)
        assert row is not None and other is not None
        row.chat_session_id = other.chat_session_id = chat_id
        consent_id, version = row.consent_id, row.version
        notification = session.scalars(
            select(EmailOutbox).where(EmailOutbox.order_id == identifier)
        ).one()
        notification.status = {
            "support": "pending",
            "commercial": "sent",
            "mixed": "failed",
            "service": "sending",
        }[kind]
        if notification.status == "sending":
            notification.lease_token = uuid4()
            notification.lease_until = utc_now() + timedelta(minutes=5)
        elif notification.status == "sent":
            notification.sent_at = utc_now()
        elif notification.status == "failed":
            notification.failed_at = utc_now()
        session.add(
            ChatMessage(
                session_id=chat_id,
                request_id=uuid4(),
                request_hash="e" * 64,
                input={"text": "Synthetic independent conversation"},
                response={"text": "Synthetic response"},
                state_version_after=1,
                matched_by="none",
                latency_ms=0,
            )
        )
    terminal(runtime, identifier, "closed" if erased else "cancelled")
    if erased:
        with runtime.unit_of_work() as session:
            row = session.get(Order, identifier)
            assert row is not None and row.terminal_at is not None
            at = row.terminal_at + timedelta(
                days=demo_bundle().retention.order_pii_after_terminal_days
            )
            pii_command = command_for(
                prepare_entry(session, "order_pii", row.id, demo_bundle(), at)
            )
        proof = witness(tmp_path, pii_command)
        with runtime.unit_of_work() as session:
            assert apply_command(session, pii_command, proof, now=at)
        with runtime.unit_of_work() as session:
            row = session.get(Order, identifier)
            assert row is not None and row.pii_erased_at is not None
            assert all(getattr(row, field) is None for field in PII_FIELDS)
    with migrator.unit_of_work() as session:
        # Snapshot deletion and consent-first deletion remain prohibited even
        # for migrator. Only the existing parent CASCADE is permitted.
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(
                text("DELETE FROM app.consents WHERE id=:id"), {"id": consent_id}
            )
        if kind != "support":
            with pytest.raises(IntegrityError), session.begin_nested():
                session.execute(
                    text("DELETE FROM app.order_items WHERE order_id=:id"),
                    {"id": identifier},
                )
        before = graph(session)
        texts = list(session.execute(select(ConsentText.__table__)))
        publications = list(session.execute(select(ConsentPublication.__table__)))
        chat = list(session.execute(select(ChatSession.__table__)))
        messages = list(session.execute(select(ChatMessage.__table__)))
        code, command = prepare_deletion(
            session,
            login="privacy-admin",
            password=PASSWORD,
            order_id=identifier,
            expected_version=version,
            decision_id=uuid4(),
            journal_policy_version=DEMO_VERSION,
        )
        assert code == "PREPARED" and command is not None
    with pytest.raises(JournalUnavailable):
        with migrator.unit_of_work() as session:
            apply_command(session, command, b"", manual_authorized=True)
    proof = witness(tmp_path, command)
    with pytest.raises(JournalUnavailable):
        with runtime.unit_of_work() as session:
            apply_command(session, command, proof, manual_authorized=True)
    with pytest.raises(JournalUnavailable):
        with migrator.unit_of_work() as session:
            apply_command(session, command, proof)

    def fail_commit(connection: Connection) -> None:
        connection.exec_driver_sql("SELECT 1/0")

    if failure == "during_commit":
        event.listen(migrator.engine, "commit", fail_commit)
    try:
        if failure == "after_commit":
            with migrator.unit_of_work() as session:
                assert apply_command(session, command, proof, manual_authorized=True)
        else:
            with pytest.raises(
                RuntimeError if failure == "before_commit" else DBAPIError
            ):
                with migrator.unit_of_work() as session:
                    assert apply_command(
                        session, command, proof, manual_authorized=True
                    )
                    assert session.get(Consent, consent_id) is None
                    if failure == "before_commit":
                        raise RuntimeError("Synthetic crash after full graph mutation")
    finally:
        if failure == "during_commit":
            event.remove(migrator.engine, "commit", fail_commit)

    with migrator.unit_of_work() as session:
        entry = session.get(DeletionEntry, command.id)
        assert entry is not None
        assert (entry.applied_at is not None) == (failure == "after_commit")
        if failure != "after_commit":
            assert graph(session) == before
        assert apply_command(session, command, proof, manual_authorized=True) == (
            failure != "after_commit"
        )
    # Simulate lost post-commit acknowledgement: same external command, no
    # second mutation, no new evidence/children and no surviving graph fragment.
    assert (tmp_path / (str(command.id) + ".json")).read_bytes() == proof
    with migrator.unit_of_work() as session:
        assert not apply_command(session, command, proof, manual_authorized=True)
        after = graph(session)
        assert after["orders"] == [r for r in before["orders"] if r.id != identifier]
        assert after["consents"] == [
            r for r in before["consents"] if r.id != consent_id
        ]
        for table in ("order_items", "order_status_history", "email_outbox"):
            assert after[table] == [
                r for r in before[table] if r.order_id != identifier
            ]
        assert list(session.execute(select(ConsentText.__table__))) == texts
        assert (
            list(session.execute(select(ConsentPublication.__table__))) == publications
        )
        assert list(session.execute(select(ChatSession.__table__))) == chat
        assert list(session.execute(select(ChatMessage.__table__))) == messages
        assert (
            session.scalar(
                select(AuditEvent.id).where(AuditEvent.action == "privacy.order_delete")
            )
            is not None
        )
        other = session.get(Order, sibling)
        assert other is not None and other.chat_session_id == chat_id
        comment = other.comment
        conversation = session.get(ChatSession, chat_id)
        assert conversation is not None
        at = conversation.created_at + timedelta(
            days=demo_bundle().retention.chat_storage_days
        )
        chat_command = command_for(
            prepare_entry(session, "chat_delete", chat_id, demo_bundle(), at)
        )
    chat_proof = witness(tmp_path, chat_command)
    with runtime.unit_of_work() as session:
        assert apply_command(session, chat_command, chat_proof, now=at)
    with runtime.unit_of_work() as session:
        assert session.get(ChatSession, chat_id) is None
        assert session.scalar(select(ChatMessage.id)) is None
        other = session.get(Order, sibling)
        assert other is not None and other.chat_session_id is None
        assert other.comment == comment and other.pii_erased_at is None


@pytest.mark.parametrize("kind", ["support", "commercial", "mixed", "service"])
def test_elapsed_snapshot_periods_do_not_authorize_full_delete(
    privacy_db: tuple[Database, Database], client: TestClient, kind: str
) -> None:
    runtime, _ = privacy_db
    identifier = create(client, runtime, kind)
    at = terminal(runtime, identifier) + timedelta(days=1000)
    with runtime.unit_of_work() as session:
        before = graph(session)
        prepare_batch(session, "order_pii", now=at)
        commands = list(session.scalars(select(DeletionEntry)))
        assert commands and all(row.operation == "order_pii" for row in commands)
        assert graph(session) == before
        if kind != "support":
            assert session.scalar(select(OrderItem.id)) is not None


@pytest.mark.parametrize("format", [1, 2])
def test_existing_manual_journal_formats_delete_same_graph(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    format: int,
) -> None:
    runtime, migrator = privacy_db
    identifier = create(client, runtime, "mixed")
    with migrator.unit_of_work() as session:
        row = session.get(Order, identifier)
        assert row is not None
        at = utc_now()
        entry = DeletionEntry(
            format=format,
            operation="order_delete",
            object_id=identifier,
            consent_id=row.consent_id,
            decision_id=uuid4(),
            policy_version=DEMO_VERSION if format == 1 else None,
            journal_policy_version=DEMO_VERSION if format == 2 else None,
            created_at=at,
            retain_until=at
            + timedelta(days=demo_bundle().retention.deletion_journal_days),
        )
        session.add(entry)
        session.flush()
        command = command_for(entry)
    proof = witness(tmp_path, command)
    with migrator.unit_of_work() as session:
        assert apply_command(session, command, proof, manual_authorized=True)
    with migrator.unit_of_work() as session:
        assert all(not rows for rows in graph(session).values())
        persisted = session.get(DeletionEntry, command.id)
        assert persisted is not None and persisted.applied_at is not None
        assert command_for(persisted).canonical() == proof
        assert not apply_command(session, command, proof, manual_authorized=True)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE app.consent_texts SET text='Synthetic forbidden'",
        "DELETE FROM app.consent_texts",
        "UPDATE app.consent_publications SET bundle_checksum=repeat('0',64)",
        "DELETE FROM app.consent_publications",
    ],
)
def test_full_delete_does_not_require_mutable_policy_evidence(
    privacy_db: tuple[Database, Database], statement: str
) -> None:
    with pytest.raises(IntegrityError):
        with privacy_db[1].unit_of_work() as session:
            session.execute(text(statement))
