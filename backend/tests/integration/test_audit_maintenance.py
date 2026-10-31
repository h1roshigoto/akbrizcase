"""PG16 authority boundary and fail-closed unbound historical retention.

Positive mutation tests inject ONLY synthetic service applicability at the
unimplemented binding seam. They prove runner mechanics, not an approved binding
or permission to delete any currently unbound service record.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session
from test_cleanup import create, terminal, witness
from test_cleanup import isolated_cleanup as isolated_cleanup
from test_cleanup_review import legacy_order
from test_privacy import PASSWORD, packet, publish
from test_privacy import client as client
from test_privacy import privacy_db as privacy_db

from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff import disable_staff
from app.core.staff_models import AuditEvent, StaffUser
from app.modules.chat.models import ChatSession
from app.modules.notify.models import EmailOutbox
from app.modules.orders.audit_maintenance import (
    audit_maintenance_tick,
    authorize_maintenance,
)
from app.modules.orders.cleanup_service import (
    apply_command,
    pending_commands,
    prepare_batch,
    prepare_entry,
    try_lock,
)
from app.modules.orders.deletion_journal import (
    JournalUnavailable,
    SyntheticJournal,
    command_for,
)
from app.modules.orders.deletion_models import DeletionEntry
from app.modules.orders.deletion_reconciliation import JournalState, reconcile_command
from app.modules.orders.maintenance_authority import has_audit_authority
from app.modules.orders.models import Consent, ConsentText, Order
from app.modules.orders.privacy_models import ConsentPublication
from app.modules.orders.retention_review import (
    aggregate_retention_report,
    retention_report,
)
from app.modules.orders.seed import demo_bundle
from app.worker.cleanup import cleanup_tick

pytestmark = pytest.mark.integration


def technical_event(database: Database) -> AuditEvent:
    with database.unit_of_work() as session:
        row = AuditEvent(
            action="staff.login",
            entity_type="staff_user",
            result="success",
            at=utc_now() - timedelta(days=1000),
            details={"synthetic": "DO-NOT-REPORT synthetic@example.invalid"},
        )
        session.add(row)
        session.flush()
        return row


def test_roles_and_unbound_runner_have_no_new_bypass(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
) -> None:
    runtime, migrator = privacy_db
    row = technical_event(runtime)
    with runtime.unit_of_work() as session:
        assert not session.scalar(
            text("SELECT has_table_privilege(current_user,'app.audit_events','DELETE')")
        )
    with pytest.raises(ProgrammingError), runtime.unit_of_work() as session:
        session.execute(delete(AuditEvent).where(AuditEvent.id == row.id))
    with migrator.unit_of_work() as session:
        assert session.scalar(
            text("SELECT has_table_privilege(current_user,'app.audit_events','DELETE')")
        )
        command = command_for(
            prepare_entry(session, "audit_delete", row.id, demo_bundle(), utc_now())
        )
    sink = SyntheticJournal(tmp_path)
    proof = witness(tmp_path, command)
    with pytest.raises(JournalUnavailable), migrator.unit_of_work() as session:
        apply_command(session, command, proof)
    assert (
        reconcile_command(
            migrator,
            command,
            sink,
            execute=True,
        ).state
        == JournalState.REVIEW_REQUIRED
    )
    assert (
        reconcile_command(
            migrator,
            command,
            sink,
            execute=True,
            authorize_audit=lambda _: True,
        ).state
        == JournalState.REVIEW_REQUIRED
    )
    # Even with correct authority, neither old PREPARED nor a durable witness
    # manufactures the missing service binding.
    result = audit_maintenance_tick(
        migrator,
        sink,
        login="privacy-admin",
        password=PASSWORD,
    )
    assert result["REVIEW_REQUIRED"] == 1
    with migrator.unit_of_work() as session:
        assert session.get(AuditEvent, row.id) is not None
        assert session.get(DeletionEntry, command.id).applied_at is None  # type: ignore[union-attr]
    cleanup_tick(migrator, journal=sink)
    with migrator.unit_of_work() as session:
        assert session.get(AuditEvent, row.id) is not None
        assert pending_commands(session)[0] == []


@pytest.mark.parametrize(
    "denial", ["manager", "inactive", "password", "runtime", "production"]
)
def test_maintenance_authority_denial_is_audited(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    denial: str,
) -> None:
    runtime, migrator = privacy_db
    row = technical_event(runtime)
    if denial == "inactive":
        with migrator.unit_of_work() as session:
            session.execute(
                update(StaffUser)
                .where(StaffUser.login == "privacy-admin")
                .values(is_active=False)
            )
    if denial == "production":
        monkeypatch.setenv("APP_ENV", "production")
    result = audit_maintenance_tick(
        runtime if denial == "runtime" else migrator,
        SyntheticJournal(tmp_path),
        login="privacy-manager" if denial == "manager" else "privacy-admin",
        password="wrong-synthetic" if denial == "password" else PASSWORD,
    )
    assert result == {"AUTHORITY_DENIED": 1}
    with migrator.unit_of_work() as session:
        assert session.get(AuditEvent, row.id) is not None
        assert not list(session.scalars(select(DeletionEntry)))
        assert (
            session.scalar(
                select(AuditEvent.id).where(
                    AuditEvent.action == "privacy.audit_maintenance",
                    AuditEvent.result != "success",
                )
            )
            is not None
        )
    assert not list(tmp_path.iterdir())


def test_synthetic_runner_pipeline_with_fixture_service_binding(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime, migrator = privacy_db
    row = technical_event(runtime)
    at = utc_now()
    preserved = [
        "expert.review",
        "catalog.applicability.confirm",
        "privacy.publish",
        "security.alert",
        "unknown.future.action",
    ]
    with runtime.unit_of_work() as session:
        for action in preserved:
            session.add(
                AuditEvent(
                    action=action, entity_type="test", result="success", at=row.at
                )
            )
        for action, result_code in (
            ("staff.login", "authentication_denied"),
            ("staff.csrf", "csrf_denied"),
            ("staff.access", "forbidden"),
        ):
            session.add(
                AuditEvent(
                    action=action,
                    entity_type="staff_user",
                    result=result_code,
                    at=row.at,
                )
            )
        unrelated = prepare_entry(session, "chat_delete", uuid4(), demo_bundle(), at)
        unrelated_id = unrelated.id
    # No new production binding/override exists. This single test seam models
    # proven applicability to exercise the protected mutation machinery.
    monkeypatch.setattr(
        "app.modules.orders.cleanup_service.shared_policy", lambda *_: demo_bundle()
    )
    sink = SyntheticJournal(tmp_path)
    result = audit_maintenance_tick(
        migrator,
        sink,
        login="privacy-admin",
        password=PASSWORD,
        now=at,
    )
    assert result["APPLIED"] == 1
    with migrator.unit_of_work() as session:
        assert session.get(AuditEvent, row.id) is None
        assert set(session.scalars(select(AuditEvent.action))) >= set(preserved)
        for action in ("staff.login", "staff.csrf", "staff.access"):
            assert (
                session.scalar(
                    select(AuditEvent.id).where(
                        AuditEvent.action == action,
                        AuditEvent.result != "success",
                    )
                )
                is not None
            )
        assert session.get(DeletionEntry, unrelated_id).applied_at is None  # type: ignore[union-attr]
        command = command_for(
            session.scalars(
                select(DeletionEntry).where(DeletionEntry.operation == "audit_delete")
            ).one()
        )
    before = sink.read(command.id, synthetic=True)
    assert reconcile_command(
        migrator,
        command,
        sink,
        execute=True,
        authorize_audit=lambda s: (
            authorize_maintenance(
                s, "privacy-admin", PASSWORD, action="privacy.audit_maintenance"
            )
            == "SUCCESS"
        ),
    ).commit_confirmed
    assert before == command.canonical() == sink.read(command.id, synthetic=True)
    assert "DO-NOT-REPORT" not in caplog.text and PASSWORD not in caplog.text


@pytest.mark.parametrize(
    "failure", ["partial", "mismatch", "outage", "receipt_only", "lost_ack"]
)
def test_protected_audit_retry_sink_failures_and_ack_loss(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    runtime, migrator = privacy_db
    row = technical_event(runtime)
    at = utc_now()
    with migrator.unit_of_work() as session:
        command = command_for(
            prepare_entry(session, "audit_delete", row.id, demo_bundle(), at)
        )
    monkeypatch.setattr(
        "app.modules.orders.cleanup_service.shared_policy", lambda *_: demo_bundle()
    )
    sink = SyntheticJournal(tmp_path)
    target = tmp_path / (str(command.id) + ".json")
    if failure in {"partial", "mismatch"}:
        target.write_bytes(command.canonical()[:10] if failure == "partial" else b"{}")
        target.chmod(0o600)

    def auth(session: Session) -> bool:
        return (
            authorize_maintenance(
                session, "privacy-admin", PASSWORD, action="privacy.audit_maintenance"
            )
            == "SUCCESS"
        )

    if failure == "outage":
        with patch.object(sink, "read", side_effect=OSError("synthetic outage")):
            result = reconcile_command(
                migrator, command, sink, execute=True, authorize_audit=auth, now=at
            )
    elif failure in {"receipt_only", "lost_ack"}:
        original = sink.export

        def export(*args: object, **kwargs: object) -> object:
            receipt = original(command, synthetic=True)
            if failure == "receipt_only":
                target.unlink()
                return receipt
            raise OSError("synthetic lost acknowledgement")

        with patch.object(sink, "export", side_effect=export):
            result = reconcile_command(
                migrator, command, sink, execute=True, authorize_audit=auth, now=at
            )
    else:
        result = reconcile_command(
            migrator, command, sink, execute=True, authorize_audit=auth, now=at
        )
    assert not result.commit_confirmed
    with migrator.unit_of_work() as session:
        assert session.get(AuditEvent, row.id) is not None
    if failure == "lost_ack":
        assert reconcile_command(
            migrator, command, sink, execute=True, authorize_audit=auth, now=at
        ).commit_confirmed


def test_runner_lock_stop_and_concurrent_invocations(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, migrator = privacy_db
    row = technical_event(runtime)
    sink = SyntheticJournal(tmp_path)
    assert audit_maintenance_tick(
        migrator, sink, stopped=lambda: True, login="privacy-admin", password=PASSWORD
    ) == {"STOPPED": 1}
    monkeypatch.setattr(
        "app.modules.orders.cleanup_service.shared_policy", lambda *_: demo_bundle()
    )
    with runtime.unit_of_work() as session:
        assert try_lock(session)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(
                audit_maintenance_tick,
                migrator,
                sink,
                login="privacy-admin",
                password=PASSWORD,
            ).result(10)
        assert result.get("APPLIED", 0) == 0
        assert session.get(AuditEvent, row.id) is not None
    deleted: list[int] = []

    def observed_delete(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if statement.startswith("DELETE FROM app.audit_events"):
            deleted.append(1)

    event.listen(migrator.engine, "after_cursor_execute", observed_delete)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: audit_maintenance_tick(
                        migrator, sink, login="privacy-admin", password=PASSWORD
                    ),
                    range(2),
                )
            )
    finally:
        event.remove(migrator.engine, "after_cursor_execute", observed_delete)
    # Both callers may truthfully reconcile the same confirmed completion.
    assert 1 <= sum(r.get("APPLIED", 0) for r in results) <= 2
    assert len(deleted) == 1
    with migrator.unit_of_work() as session:
        assert session.get(AuditEvent, row.id) is None
        assert len(list(session.scalars(select(DeletionEntry)))) == 1


def test_report_is_bounded_aggregate_read_only_and_no_fabricated_evidence(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime, _ = privacy_db
    identifier = create(client, runtime, "mixed")
    at = terminal(runtime, identifier) + timedelta(days=400)
    technical_event(runtime)
    with runtime.unit_of_work() as session:
        row = session.get(Order, identifier)
        assert row is not None
        row.terminal_at = None
        outbox = session.scalars(
            select(EmailOutbox).where(EmailOutbox.order_id == identifier)
        ).one()
        outbox.status, outbox.failed_at = "failed", None
        before = (row.consent_id, row.terminal_at, outbox.failed_at, row.pii_erased_at)
        report = retention_report(session, now=at, limit=100)
        assert report["order_pii"]["reasons"] == {"ORDER_TERMINAL_EVIDENCE": 1}
        assert report["outbox_delete"]["reasons"] == {"OUTBOX_TERMINAL_EVIDENCE": 1}
        assert (
            row.consent_id,
            row.terminal_at,
            outbox.failed_at,
            row.pii_erased_at,
        ) == before
        assert not list(session.scalars(select(DeletionEntry)))

        bounded = retention_report(session, now=at, limit=1)
        assert bounded["audit_delete"]["truncated"] is True
    serialized = json.dumps(report) + caplog.text
    for forbidden in (
        "DO-NOT-REPORT",
        "synthetic@example.invalid",
        "test@example.invalid",
        PASSWORD,
        str(identifier),
        "Synthetic test",
        "token",
        "hash",
    ):
        assert forbidden not in serialized


def test_report_pages_do_not_repeat_or_leak_cursors(
    privacy_db: tuple[Database, Database],
) -> None:
    runtime, _ = privacy_db
    for _ in range(4):
        technical_event(runtime)
    with runtime.unit_of_work() as session:
        total = session.scalar(select(text("count(*)")).select_from(AuditEvent))
        full = aggregate_retention_report(session, limit=2, max_batches=10)
        assert full["audit_delete"]["inspected"] == total
        assert full["audit_delete"]["truncated"] is False
        partial = aggregate_retention_report(session, limit=1, max_batches=1)
        assert partial["audit_delete"]["inspected"] == 1
        assert partial["audit_delete"]["truncated"] is True
        assert "cursor" not in json.dumps(full)


@pytest.mark.parametrize("state", ["sole", "missing", "ambiguous", "corrupt"])
def test_historical_policy_diagnosis_never_rebinds_or_deletes(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
    state: str,
) -> None:
    runtime, migrator = privacy_db
    event = technical_event(runtime)
    version = None
    if state in {"ambiguous", "corrupt"}:
        path, bundle = packet(tmp_path)
        if state == "ambiguous":
            assert publish(migrator, path, bundle).ok
        else:
            version = bundle.consent.version
            with migrator.unit_of_work() as session:
                session.add(
                    ConsentText(**bundle.consent.model_dump(), active_from=utc_now())
                )
                session.flush()
                session.add(
                    ConsentPublication(
                        consent_version=version,
                        bundle=bundle.model_dump(mode="json"),
                        bundle_checksum="0" * 64,
                        published_at=utc_now(),
                    )
                )
    identifier, consent_id = legacy_order(
        runtime,
        text_version=version,
        granted_at=utc_now() if version else None,
    )
    at = utc_now() + timedelta(days=400)
    with runtime.unit_of_work() as session:
        consent = session.get(Consent, consent_id)
        assert consent is not None
        original_version = consent.text_version
        created = utc_now() - timedelta(days=100)
        chat = ChatSession(
            mode="knowledge",
            owner_token_hash="e" * 64,
            created_at=created,
            last_activity_at=created,
            expires_at=created + timedelta(days=1),
        )
        session.add(chat)
        session.flush()
        if state == "missing":
            # Service-wide missing publication is covered independently below;
            # this legacy order has no publication even beside a sole packet.
            assert session.get(ConsentPublication, original_version) is None
        report = retention_report(session, now=at)
        expected = "POLICY_INVALID" if state == "corrupt" else "POLICY_MISSING"
        assert report["order_pii"]["reasons"] == {expected: 1}
        assert report["chat_delete"]["reasons"] == {"POLICY_UNBOUND": 1}
        if state == "ambiguous" or state == "corrupt":
            reasons = report["audit_delete"]["reasons"]
            assert isinstance(reasons, dict) and "POLICY_AMBIGUOUS" in reasons
        else:
            reasons = report["audit_delete"]["reasons"]
            assert isinstance(reasons, dict) and "SERVICE_BINDING_UNKNOWN" in reasons
        for category in ("order_pii", "chat_delete", "outbox_delete"):
            assert prepare_batch(session, category, now=at).processed == 0
        with pytest.raises(JournalUnavailable):
            prepare_batch(session, "audit_delete", now=at)
        assert session.get(Order, identifier).pii_erased_at is None  # type: ignore[union-attr]
        assert (
            consent.text_version == original_version and chat.retention_version is None
        )
        assert session.get(AuditEvent, event.id) is not None


def test_linked_snapshots_report_is_a_legal_decision_not_anonymity(
    privacy_db: tuple[Database, Database],
    client: TestClient,
) -> None:
    runtime, _ = privacy_db
    identifier = create(client, runtime, "service")
    at = terminal(runtime, identifier) + timedelta(days=400)
    with runtime.unit_of_work() as session:
        report = retention_report(session, now=at)
        assert report["order_pii"]["reasons"] == {"LINKED_SNAPSHOTS_LEGAL_DECISION": 1}
        assert session.get(Order, identifier) is not None
        assert not list(session.scalars(select(DeletionEntry)))


def test_audit_operator_revocation_after_export_blocks_apply(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, migrator = privacy_db
    row = technical_event(runtime)
    with migrator.unit_of_work() as session:
        command = command_for(
            prepare_entry(session, "audit_delete", row.id, demo_bundle(), utc_now())
        )
    monkeypatch.setattr(
        "app.modules.orders.cleanup_service.shared_policy", lambda *_: demo_bundle()
    )
    sink = SyntheticJournal(tmp_path)
    original = sink.export

    def revoke(*args: object, **kwargs: object) -> object:
        receipt = original(command, synthetic=True)
        with migrator.unit_of_work() as session:
            session.execute(
                update(StaffUser)
                .where(StaffUser.login == "privacy-admin")
                .values(is_active=False)
            )
        return receipt

    with patch.object(sink, "export", side_effect=revoke):
        result = reconcile_command(
            migrator,
            command,
            sink,
            execute=True,
            authorize_audit=lambda s: (
                authorize_maintenance(
                    s, "privacy-admin", PASSWORD, action="privacy.audit_maintenance"
                )
                == "SUCCESS"
            ),
        )
    assert result.state == JournalState.REVIEW_REQUIRED
    assert sink.read(command.id, synthetic=True) == command.canonical()
    with runtime.unit_of_work() as session:
        assert session.get(AuditEvent, row.id) is not None
        assert session.get(DeletionEntry, command.id).applied_at is None  # type: ignore[union-attr]


def test_operator_authority_does_not_survive_transaction_commit(
    privacy_db: tuple[Database, Database],
) -> None:
    with privacy_db[1].sessions() as session:
        with session.begin():
            assert (
                authorize_maintenance(
                    session,
                    "privacy-admin",
                    PASSWORD,
                    action="privacy.audit_maintenance",
                )
                == "SUCCESS"
            )
            assert has_audit_authority(session)
        with session.begin():
            assert not has_audit_authority(session)


def test_review_snapshot_cannot_reauthorize_an_already_disabled_admin(
    privacy_db: tuple[Database, Database],
) -> None:
    migrator = privacy_db[1]
    with pytest.raises(OperationalError), migrator.unit_of_work() as stale:
        stale.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        identifier = stale.scalar(
            select(StaffUser.id).where(StaffUser.login == "privacy-admin")
        )
        assert identifier is not None
        with migrator.unit_of_work() as writer:
            assert disable_staff(
                writer,
                admin_login="privacy-admin",
                admin_password=PASSWORD,
                user_id=identifier,
            ).ok
        authorize_maintenance(
            stale, "privacy-admin", PASSWORD, action="privacy.retention_review"
        )


def test_shutdown_after_export_leaves_exact_pending_command_for_retry(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, migrator = privacy_db
    row = technical_event(runtime)
    monkeypatch.setattr(
        "app.modules.orders.cleanup_service.shared_policy", lambda *_: demo_bundle()
    )
    sink = SyntheticJournal(tmp_path)
    stopped = Event()
    original = sink.export

    def interrupt(*args: object, **kwargs: object) -> object:
        receipt = original(*args, **kwargs)  # type: ignore[arg-type]
        stopped.set()
        return receipt

    with patch.object(sink, "export", side_effect=interrupt):
        result = audit_maintenance_tick(
            migrator,
            sink,
            login="privacy-admin",
            password=PASSWORD,
            stopped=stopped.is_set,
        )
    assert result["REVIEW_REQUIRED"] == 1
    with migrator.unit_of_work() as session:
        assert session.get(AuditEvent, row.id) is not None
        entry = session.scalars(select(DeletionEntry)).one()
        command = command_for(entry)
        assert entry.applied_at is None
    before = sink.read(command.id, synthetic=True)
    result = audit_maintenance_tick(
        migrator, sink, login="privacy-admin", password=PASSWORD
    )
    assert result["APPLIED"] == 1
    assert sink.read(command.id, synthetic=True) == before == command.canonical()
