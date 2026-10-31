"""PG16 retention, write-ahead journal, protected deletion and concurrency."""

import os
import selectors
import signal
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from test_order_creation import commercial
from test_privacy import PASSWORD, body, headers, packet, publish
from test_privacy import client as client
from test_privacy import privacy_db as privacy_db

from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff_models import AuditEvent, StaffSession, StaffUser
from app.modules.catalog.seed import seed_catalog
from app.modules.chat.models import ChatMessage, ChatSession
from app.modules.notify.models import EmailOutbox
from app.modules.orders.cleanup_service import (
    apply_command,
    eligibility,
    policy_for,
    prepare_batch,
    prepare_entry,
    try_lock,
)
from app.modules.orders.deletion_journal import (
    DeletionCommand,
    JournalUnavailable,
    SyntheticJournal,
    VerifiedWitness,
    command_for,
)
from app.modules.orders.deletion_models import DeletionEntry
from app.modules.orders.deletion_operator import preflight, prepare_deletion
from app.modules.orders.deletion_reconciliation import JournalState, reconcile_command
from app.modules.orders.erasure_service import PII_FIELDS, erase_order_pii
from app.modules.orders.models import (
    Consent,
    ConsentText,
    Order,
    OrderItem,
    OrderStatusHistory,
)
from app.modules.orders.privacy_models import ConsentPublication
from app.modules.orders.privacy_service import PrivacyUnavailable
from app.modules.orders.seed import DEMO_VERSION, demo_bundle
from app.worker.cleanup import cleanup_tick

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def isolated_cleanup(privacy_db: tuple[Database, Database]) -> Iterator[None]:
    with privacy_db[1].unit_of_work() as session:
        session.execute(text("TRUNCATE app.deletion_journal, app.cleanup_cursors"))
    yield


def create(client: TestClient, database: Database, kind: str = "support") -> UUID:
    seed_catalog(database, mode="test")
    skus = {
        "commercial": ("DEMO-P-001",),
        "mixed": ("DEMO-P-001", "DEMO-S-001"),
        "service": ("DEMO-S-001",),
    }
    request = commercial(database, skus[kind]) if kind != "support" else body()
    response = client.post("/api/v1/orders", json=request, headers=headers())
    assert response.status_code == 201, response.text
    with database.unit_of_work() as session:
        return session.scalars(
            select(Order.id).where(Order.number == response.json()["number"])
        ).one()


def terminal(database: Database, identifier: UUID, status: str = "closed") -> datetime:
    with database.unit_of_work() as session:
        row = session.get(Order, identifier)
        assert row is not None
        at = row.created_at + timedelta(seconds=1)
        row.status, row.terminal_at = status, at
        row.closed_at = at if status == "closed" else None
        session.add(
            OrderStatusHistory(
                order_id=row.id,
                from_status="in_progress" if status == "closed" else "new",
                to_status=status,
                at=at,
                reason="Synthetic private reason",
            )
        )
        return at


def prepare(database: Database, identifier: UUID, now: datetime) -> DeletionCommand:
    with database.unit_of_work() as session:
        row = session.get(Order, identifier)
        assert row is not None
        bundle, due, _ = eligibility(session, "order_pii", row, now)
        assert due
        return command_for(prepare_entry(session, "order_pii", identifier, bundle, now))


def witness(tmp_path: Path, command: DeletionCommand) -> VerifiedWitness:
    tmp_path.chmod(0o700)
    return SyntheticJournal(tmp_path).witness(command, synthetic=True)


@pytest.mark.parametrize("kind", ["support", "commercial", "mixed", "service"])
@pytest.mark.parametrize("status", ["closed", "cancelled"])
def test_exact_pii_boundary_and_snapshots_retained(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    kind: str,
    status: str,
) -> None:
    database = privacy_db[0]
    identifier = create(client, database, kind)
    at = terminal(database, identifier, status)
    deadline = at + timedelta(
        days=demo_bundle().retention.order_pii_after_terminal_days
    )
    with database.unit_of_work() as session:
        row = session.get(Order, identifier)
        assert row is not None
        identity = (row.id, row.number, row.consent_id, row.idempotency_key)
        for offset in (-1, 0, 1):
            _, due, review = eligibility(
                session, "order_pii", row, deadline + timedelta(seconds=offset)
            )
            assert due == (offset >= 0)
            assert review == (
                kind != "support"
                and at
                + timedelta(
                    days=demo_bundle().retention.linked_snapshots_after_terminal_days
                )
                <= deadline + timedelta(seconds=offset)
            )
        snapshots = list(
            session.execute(
                select(OrderItem.__table__).where(OrderItem.order_id == identifier)
            )
        )
    command = prepare(database, identifier, deadline)
    proof = witness(tmp_path, command)
    with database.unit_of_work() as session:
        assert apply_command(session, command, proof, now=deadline)
    with database.unit_of_work() as session:
        row = session.get(Order, identifier)
        assert row is not None and row.pii_erased_at == deadline
        assert all(getattr(row, name) is None for name in PII_FIELDS)
        assert (row.id, row.number, row.consent_id, row.idempotency_key) == identity
        assert (
            session.scalar(
                select(OrderStatusHistory.reason).where(
                    OrderStatusHistory.order_id == identifier,
                    OrderStatusHistory.reason.is_not(None),
                )
            )
            is None
        )
        assert (
            list(
                session.execute(
                    select(OrderItem.__table__).where(OrderItem.order_id == identifier)
                )
            )
            == snapshots
        )
        assert session.get(Consent, row.consent_id) is not None
        assert not apply_command(session, command, proof, now=deadline)


@pytest.mark.parametrize("failure", ["before-commit", "during-commit", "after-commit"])
def test_crash_boundaries_restore_or_retain_atomic_effect(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    failure: str,
) -> None:
    database = privacy_db[0]
    identifier = create(client, database)
    now = terminal(database, identifier) + timedelta(days=181)
    command = prepare(database, identifier, now)
    proof = witness(tmp_path, command)

    def fail_commit(connection: Connection) -> None:
        connection.exec_driver_sql("SELECT 1/0")

    if failure == "during-commit":
        event.listen(database.engine, "commit", fail_commit)
    try:
        if failure == "after-commit":
            with database.unit_of_work() as session:
                assert apply_command(session, command, proof, now=now)
        else:
            with pytest.raises(
                RuntimeError if failure == "before-commit" else DBAPIError
            ):
                with database.unit_of_work() as session:
                    assert apply_command(session, command, proof, now=now)
                    if failure == "before-commit":
                        raise RuntimeError("synthetic crash")
    finally:
        if failure == "during-commit":
            event.remove(database.engine, "commit", fail_commit)
    with database.unit_of_work() as session:
        row = session.get(Order, identifier)
        entry = session.get(DeletionEntry, command.id)
        assert row is not None and entry is not None
        assert (row.pii_erased_at is not None) == (failure == "after-commit")
        assert (entry.applied_at is not None) == (failure == "after-commit")
        assert (row.contact_name is None) == (failure == "after-commit")
    assert (tmp_path / (str(command.id) + ".json")).read_bytes() == proof.canonical
    with database.unit_of_work() as session:
        assert apply_command(session, command, proof, now=now) == (
            failure != "after-commit"
        )


def test_missing_witness_bad_packet_and_production_deny(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = privacy_db[0]
    identifier = create(client, database)
    now = terminal(database, identifier) + timedelta(days=181)
    command = prepare(database, identifier, now)
    with pytest.raises(JournalUnavailable):
        with database.unit_of_work() as session:
            apply_command(session, command, b"", now=now)
    with patch.object(SyntheticJournal, "export", side_effect=OSError("synthetic")):
        cleanup_tick(database, now=now, journal=SyntheticJournal(tmp_path))
    with database.unit_of_work() as session:
        row = session.get(Order, identifier)
        assert row is not None and row.pii_erased_at is None
    monkeypatch.setenv("APP_ENV", "production")
    with database.unit_of_work() as session, pytest.raises(PrivacyUnavailable):
        policy_for(session, DEMO_VERSION, now)
    with database.unit_of_work() as session, pytest.raises(JournalUnavailable):
        apply_command(session, command, command.canonical(), now=now)


def test_two_workers_skip_locked_and_restart_cursor(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
) -> None:
    database = privacy_db[0]
    identifiers = [create(client, database) for _ in range(3)]
    now = max(terminal(database, identifier) for identifier in identifiers) + timedelta(
        days=181
    )
    with database.unit_of_work() as session:
        assert prepare_batch(session, "order_pii", now=now, limit=1).processed == 1
    # A new UoW resumes committed keyset progress, including after downtime.
    with database.unit_of_work() as session:
        assert prepare_batch(session, "order_pii", now=now, limit=1).processed == 1
    cleanup_tick(database, now=now, journal=SyntheticJournal(tmp_path))
    with database.unit_of_work() as session:
        commands = list(session.scalars(select(DeletionEntry)))
        assert len(commands) == 3
        assert all(row.applied_at is not None for row in commands)
    # Explicit contention proves a second planner cannot overlap.
    with database.unit_of_work() as session:
        assert try_lock(session)
        with ThreadPoolExecutor(max_workers=1) as pool:

            def attempt() -> int:
                with database.unit_of_work() as other:
                    return prepare_batch(other, "order_pii", now=now).skipped

            assert pool.submit(attempt).result(timeout=3) == 1
    # Applying one witnessed command concurrently has exactly one effect.
    extra = create(client, database)
    later = terminal(database, extra) + timedelta(days=181)
    command = prepare(database, extra, later)
    proof = witness(tmp_path, command)

    def apply() -> bool:
        with database.unit_of_work() as session:
            return apply_command(session, command, proof, now=later)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: apply(), range(2))) == [False, True]


def test_open_legacy_terminal_and_unbound_chat_require_review(
    privacy_db: tuple[Database, Database],
    client: TestClient,
) -> None:
    database = privacy_db[0]
    identifier = create(client, database)
    now = utc_now() + timedelta(days=400)
    with database.unit_of_work() as session:
        assert prepare_batch(session, "order_pii", now=now).manual_review == 1
        row = session.get(Order, identifier)
        assert row is not None and row.pii_erased_at is None
        row.status = "cancelled"
    with database.unit_of_work() as session:
        row = session.get(Order, identifier)
        assert row is not None
        assert eligibility(session, "order_pii", row, now)[1:] == (False, True)
        session.add(
            ChatSession(
                owner_token_hash="e" * 64,
                mode="knowledge",
                created_at=now - timedelta(days=31),
                last_activity_at=now - timedelta(days=31),
                expires_at=now - timedelta(days=30),
            )
        )
    with database.unit_of_work() as session:
        assert prepare_batch(session, "chat_delete", now=now).manual_review == 1


@pytest.mark.parametrize(
    "login,code",
    [
        ("privacy-manager", "FORBIDDEN"),
        ("missing-admin", "AUTHENTICATION_DENIED"),
        ("privacy-admin", "SUCCESS"),
    ],
)
def test_manual_auth_and_runtime_boundary(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    login: str,
    code: str,
) -> None:
    identifier = create(client, privacy_db[0])
    with privacy_db[1].unit_of_work() as session:
        result, _ = preflight(
            session, login=login, password=PASSWORD, order_id=identifier
        )
        assert result == ("READY" if code == "SUCCESS" else code)
    with privacy_db[0].unit_of_work() as session:
        result, _ = preflight(
            session, login="privacy-admin", password=PASSWORD, order_id=identifier
        )
        assert result == "PROTECTED_DATABASE_ROLE_REQUIRED"
    for table in ("orders", "consents"):
        with pytest.raises(ProgrammingError):
            with privacy_db[0].unit_of_work() as session:
                session.execute(text(f"DELETE FROM app.{table}"))


def test_manual_full_delete_graph_consent_restrict_and_repeat(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
) -> None:
    identifier = create(client, privacy_db[0], "mixed")
    with privacy_db[1].unit_of_work() as session:
        row = session.get(Order, identifier)
        assert row is not None
        consent_id, version = row.consent_id, row.version
    with pytest.raises(IntegrityError):
        with privacy_db[1].unit_of_work() as session:
            session.execute(
                text("DELETE FROM app.consents WHERE id=:id"), {"id": consent_id}
            )
    with privacy_db[1].unit_of_work() as session:
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
    proof = witness(tmp_path, command)
    with privacy_db[1].unit_of_work() as session:
        assert apply_command(session, command, proof, manual_authorized=True)
    with privacy_db[1].unit_of_work() as session:
        for model in (Order, OrderItem, OrderStatusHistory, EmailOutbox, Consent):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert session.get(ConsentText, DEMO_VERSION) is not None
        assert session.get(ConsentPublication, DEMO_VERSION) is not None
        assert not apply_command(session, command, proof, manual_authorized=True)
        assert preflight(
            session, login="privacy-admin", password=PASSWORD, order_id=identifier
        ) == ("ABSENT", None)


def test_policy_version_is_not_replaced_by_new_publication_or_runtime_file(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = create(client, privacy_db[0])
    at = terminal(privacy_db[0], identifier)
    path, bundle = packet(tmp_path)
    assert publish(privacy_db[1], path, bundle).ok
    monkeypatch.setenv("PRIVACY_CONFIG_FILE", "/nonexistent/approved.json")
    with privacy_db[0].unit_of_work() as session:
        row = session.get(Order, identifier)
        assert row is not None
        old, due, _ = eligibility(session, "order_pii", row, at + timedelta(days=181))
        assert old.consent.version == DEMO_VERSION and due


def test_chat_ttl_cascade_support_independence_and_locked_session(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
) -> None:
    database = privacy_db[0]
    created = client.post("/api/v1/chat/sessions", json={"mode": "knowledge"})
    assert created.status_code == 201
    chat_id = UUID(created.json()["session_id"])
    order_id = create(client, database)
    with database.unit_of_work() as session:
        chat = session.get(ChatSession, chat_id)
        assert chat is not None and chat.retention_version == DEMO_VERSION
        now = chat.created_at + timedelta(
            days=demo_bundle().retention.chat_storage_days
        )
        session.add(
            ChatMessage(
                session_id=chat.id,
                request_id=uuid4(),
                request_hash="e" * 64,
                input={"text": "synthetic input"},
                response={"text": "synthetic response"},
                state_version_after=1,
                matched_by="none",
                latency_ms=0,
            )
        )
        row = session.get(Order, order_id)
        assert row is not None
        row.chat_session_id = chat.id
        comment, consent = row.comment, row.consent_id
        for offset in (-1, 0, 1):
            assert eligibility(
                session, "chat_delete", chat, now + timedelta(seconds=offset)
            )[1] == (offset >= 0)
        bundle, _, _ = eligibility(session, "chat_delete", chat, now)
        command = command_for(
            prepare_entry(session, "chat_delete", chat.id, bundle, now)
        )
    proof = witness(tmp_path, command)
    with database.unit_of_work() as owner:
        owner.scalar(
            select(ChatSession)
            .where(ChatSession.id == chat_id)
            .with_for_update(read=True)
        )

        def attempt() -> bool:
            with database.unit_of_work() as session:
                return apply_command(session, command, proof, now=now)

        with ThreadPoolExecutor(max_workers=1) as pool:
            assert not pool.submit(attempt).result(timeout=3)
    with database.unit_of_work() as session:
        assert apply_command(session, command, proof, now=now)
    with database.unit_of_work() as session:
        assert session.get(ChatSession, chat_id) is None
        assert session.scalar(select(func.count()).select_from(ChatMessage)) == 0
        order = session.get(Order, order_id)
        assert order is not None and order.chat_session_id is None
        assert order.comment == comment and order.consent_id == consent
        assert session.scalar(select(func.count()).select_from(EmailOutbox)) == 1


@pytest.mark.parametrize(
    "status", ["sent", "failed", "pending", "sending", "legacy_failed"]
)
def test_outbox_boundaries_and_preserved_unfinished(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    status: str,
) -> None:
    database = privacy_db[0]
    identifier = create(client, database)
    at = utc_now()
    with database.unit_of_work() as session:
        row = session.scalars(
            select(EmailOutbox).where(EmailOutbox.order_id == identifier)
        ).one()
        row.status = "failed" if status == "legacy_failed" else status
        if status == "sent":
            row.sent_at = at
        if status == "failed":
            row.failed_at = at
        if status == "sending":
            row.lease_token, row.lease_until = uuid4(), at
        now = at + timedelta(
            days=demo_bundle().retention.outbox_sent_days
            if status == "sent"
            else demo_bundle().retention.outbox_failed_days
        )
        for offset in (-1, 0, 1):
            bundle, due, review = eligibility(
                session, "outbox_delete", row, now + timedelta(seconds=offset)
            )
            assert due == (status in {"sent", "failed"} and offset >= 0)
            assert review == (status not in {"sent", "failed"})
        row_id = row.id
        if status in {"sent", "failed"}:
            command = command_for(
                prepare_entry(session, "outbox_delete", row.id, bundle, now)
            )
    if status in {"sent", "failed"}:
        proof = witness(tmp_path, command)
        with database.unit_of_work() as session:
            assert apply_command(session, command, proof, now=now)
        with database.unit_of_work() as session:
            assert session.get(EmailOutbox, row_id) is None
            assert session.get(Order, identifier) is not None


def test_sessions_and_audit_allowlist(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
) -> None:
    database, migrator = privacy_db
    at = utc_now()
    with migrator.unit_of_work() as session:
        actor = session.scalar(text("SELECT id FROM app.staff_users LIMIT 1"))
        expired = StaffSession(user_id=actor, token_hash="a" * 64, expires_at=at)
        live = StaffSession(
            user_id=actor, token_hash="b" * 64, expires_at=at + timedelta(days=2)
        )
        audit = AuditEvent(
            actor_id=actor,
            action="staff.login",
            entity_type="staff_user",
            result="success",
            details={},
            at=at - timedelta(days=demo_bundle().retention.audit_days),
        )
        protected = AuditEvent(
            actor_id=actor,
            action="catalog.applicability.confirm",
            entity_type="catalog_item",
            result="success",
            details={},
            at=at - timedelta(days=1000),
        )
        session.add_all([expired, live, audit, protected])
        session.flush()
        expired_id, live_id, audit_id, protected_id = (
            expired.id,
            live.id,
            audit.id,
            protected.id,
        )
        assert (
            eligibility(
                session, "staff_session_delete", expired, at - timedelta(seconds=1)
            )[1]
            is False
        )
        assert eligibility(session, "staff_session_delete", expired, at)[1] is True
        assert (
            eligibility(session, "audit_delete", audit, at - timedelta(seconds=1))[1]
            is False
        )
        assert eligibility(session, "audit_delete", audit, at)[1] is True
    cleanup_tick(migrator, now=at, journal=SyntheticJournal(tmp_path))
    with database.unit_of_work() as session:
        assert session.get(StaffSession, expired_id) is None
        assert session.get(StaffSession, live_id) is not None
        assert session.get(AuditEvent, audit_id) is None
        assert session.get(AuditEvent, protected_id) is not None


def test_database_prevents_partial_erasure_and_reason_reintroduction(
    privacy_db: tuple[Database, Database],
    client: TestClient,
) -> None:
    database = privacy_db[0]
    identifier = create(client, database)
    with pytest.raises(IntegrityError, match="ck_orders_erased_fields"):
        with database.unit_of_work() as session:
            session.execute(
                update(Order)
                .where(Order.id == identifier)
                .values(pii_erased_at=utc_now())
            )
    with database.unit_of_work() as session:
        assert erase_order_pii(session, identifier, utc_now())
    with pytest.raises(IntegrityError, match="Erased order cannot retain history text"):
        with database.unit_of_work() as session:
            session.add(
                OrderStatusHistory(
                    order_id=identifier,
                    to_status="closed",
                    reason="synthetic new private text",
                )
            )


@pytest.mark.parametrize("invalid_smtp", [False, True])
def test_real_worker_cleans_with_smtp_disabled_or_invalid_and_stops(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
    invalid_smtp: bool,
) -> None:
    database = privacy_db[0]
    with database.unit_of_work() as session:
        publication = session.get(ConsentPublication, DEMO_VERSION)
        assert publication is not None
        created_at = publication.published_at + timedelta(days=1)
        consent = Consent(text_version=DEMO_VERSION, granted_at=created_at)
        session.add(consent)
        session.flush()
        order = Order(
            kind="support",
            consent_id=consent.id,
            idempotency_key=uuid4(),
            contact_name="Synthetic worker test",
            email="worker@example.invalid",
            comment="Synthetic independent issue",
            request_hash="e" * 64,
            receipt_token_hash="f" * 64,
            known_subtotal=0,
            delivery_status="not_required",
            delivery_cost=0,
            status="cancelled",
            created_at=created_at,
            terminal_at=created_at + timedelta(days=1),
        )
        session.add(order)
        session.flush()
        identifier = order.id
    tmp_path.chmod(0o700)
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("SMTP_")
    }
    environment["SYNTHETIC_DELETION_JOURNAL_DIR"] = str(tmp_path)
    if invalid_smtp:
        environment["SMTP_HOST"] = "synthetic-invalid-config"
    process = subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
    )
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    lines = []
    try:
        # Real subprocess, real PG commit and real filesystem witness. A
        # bounded selector wait avoids indefinite readline on a broken worker.
        for _ in range(20):
            assert selector.select(timeout=3), "Worker progress timeout"
            chunk = os.read(process.stdout.fileno(), 65536).decode()
            assert chunk
            lines.append(chunk)
            if "CLEANUP_APPLIED" in chunk:
                break
        assert any("CLEANUP_APPLIED" in line for line in lines)
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=5)
        assert process.returncode == 0 and "Worker stopped gracefully" in output
        with database.unit_of_work() as session:
            row = session.get(Order, identifier)
            assert row is not None and row.pii_erased_at is not None
            assert row.comment is None and row.receipt_token_hash is None
        assert "worker@example.invalid" not in "".join(lines) + output
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_admin_disabled_and_wrong_password_denied(
    privacy_db: tuple[Database, Database],
    client: TestClient,
) -> None:
    identifier = create(client, privacy_db[0])
    with privacy_db[1].unit_of_work() as session:
        assert (
            preflight(
                session,
                login="privacy-admin",
                password="synthetic-wrong-password",
                order_id=identifier,
            )[0]
            == "AUTHENTICATION_DENIED"
        )
        session.execute(
            update(StaffUser)
            .where(StaffUser.login == "privacy-admin")
            .values(is_active=False)
        )
    with privacy_db[1].unit_of_work() as session:
        assert (
            preflight(
                session, login="privacy-admin", password=PASSWORD, order_id=identifier
            )[0]
            == "AUTHENTICATION_DENIED"
        )
        assert session.get(Order, identifier) is not None


def test_missing_and_corrupt_packet_do_not_authorize_cleanup(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
) -> None:
    path, bundle = packet(tmp_path)
    now = utc_now()
    with privacy_db[1].unit_of_work() as session:
        session.add(ConsentText(**bundle.consent.model_dump(), active_from=now))
        session.flush()
        session.add(
            ConsentPublication(
                consent_version=bundle.consent.version,
                bundle=bundle.model_dump(mode="json"),
                bundle_checksum="0" * 64,
                published_at=now,
            )
        )
    with privacy_db[0].unit_of_work() as session:
        for version in (None, "missing", bundle.consent.version):
            with pytest.raises(PrivacyUnavailable):
                policy_for(session, version, now)


def test_chat_messages_are_removed_in_bounded_chunks(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
) -> None:
    response = client.post("/api/v1/chat/sessions", json={"mode": "knowledge"})
    assert response.status_code == 201
    chat_id = UUID(response.json()["session_id"])
    database = privacy_db[0]
    with database.unit_of_work() as session:
        row = session.get(ChatSession, chat_id)
        assert row is not None
        now = row.created_at + timedelta(days=demo_bundle().retention.chat_storage_days)
        for _ in range(101):
            session.add(
                ChatMessage(
                    session_id=row.id,
                    request_id=uuid4(),
                    request_hash="e" * 64,
                    input={},
                    response={},
                    state_version_after=1,
                    matched_by="none",
                    latency_ms=0,
                )
            )
        bundle = policy_for(session, row.retention_version, now)
        command = command_for(
            prepare_entry(session, "chat_delete", row.id, bundle, now)
        )
    proof = witness(tmp_path, command)
    result = reconcile_command(
        database, command, SyntheticJournal(tmp_path), execute=True, now=now
    )
    assert result.state == JournalState.WITNESSED and not result.commit_confirmed
    with database.unit_of_work() as session:
        assert session.get(ChatSession, chat_id) is not None
        assert session.scalar(select(func.count()).select_from(ChatMessage)) == 1
        entry = session.get(DeletionEntry, command.id)
        assert entry is not None and entry.applied_at is None
        assert apply_command(session, command, proof, now=now)
    with database.unit_of_work() as session:
        assert session.get(ChatSession, chat_id) is None
