"""PG16 synthetic crash/reconciliation and isolated older-state reapplication."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool
from test_cleanup import create, prepare, terminal
from test_cleanup import isolated_cleanup as isolated_cleanup
from test_cleanup_option_a import GRAPH_TABLES, graph
from test_privacy import PASSWORD
from test_privacy import client as client
from test_privacy import privacy_db as privacy_db

from app.core.db import Database
from app.core.db_types import Base, utc_now
from app.modules.orders.cleanup_service import apply_command, prepare_entry
from app.modules.orders.deletion_journal import (
    DeletionCommand,
    DurableAcknowledgement,
    JournalUnavailable,
    SyntheticJournal,
    command_for,
)
from app.modules.orders.deletion_models import DeletionEntry
from app.modules.orders.deletion_operator import authorize
from app.modules.orders.deletion_reconciliation import JournalState, reconcile_command
from app.modules.orders.models import Order
from app.modules.orders.seed import demo_bundle

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("crash", ["before_export", "after_export", "partial_export"])
def test_crash_export_and_exact_redelivery(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    crash: str,
) -> None:
    database = privacy_db[0]
    identifier = create(client, database)
    now = terminal(database, identifier) + timedelta(days=181)
    command = prepare(database, identifier, now)
    tmp_path.chmod(0o700)
    sink = SyntheticJournal(tmp_path)
    if crash == "after_export":
        sink.export(command, synthetic=True)
    elif crash == "partial_export":
        (tmp_path / ".interrupted").write_bytes(command.canonical()[:20])
    result = reconcile_command(database, command, sink, now=now)
    assert result.state == (
        JournalState.WITNESSED if crash == "after_export" else JournalState.PREPARED
    )
    assert not result.commit_confirmed
    with database.unit_of_work() as session:
        row = session.get(Order, identifier)
        assert row is not None and row.pii_erased_at is None
        # Exact caller-supplied bytes are insufficient, even in synthetic mode.
        with pytest.raises(JournalUnavailable):
            apply_command(session, command, command.canonical(), now=now)

    # Neither export nor verification may retain a DB transaction/connection.
    read, export = sink.read, sink.export
    assert isinstance(database.engine.pool, QueuePool)

    def checked_read(*args: object, **kwargs: object) -> bytes | None:
        assert database.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        return read(command.id, synthetic=True)

    def checked_export(
        item: DeletionCommand, *, synthetic: bool
    ) -> DurableAcknowledgement:
        assert database.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        return export(item, synthetic=synthetic)

    with (
        patch.object(sink, "read", side_effect=checked_read),
        patch.object(sink, "export", side_effect=checked_export),
    ):
        result = reconcile_command(database, command, sink, execute=True, now=now)
    assert result.state == JournalState.APPLIED and result.commit_confirmed
    with database.unit_of_work() as session:
        entry = session.get(DeletionEntry, command.id)
        assert entry is not None
        applied_at = entry.applied_at
    assert reconcile_command(database, command, sink).commit_confirmed
    assert sink.read(command.id, synthetic=True) == command.canonical()
    with database.unit_of_work() as session:
        entry = session.get(DeletionEntry, command.id)
        assert entry is not None and entry.applied_at == applied_at


@pytest.mark.parametrize(
    "failure", ["rollback", "commit_failure", "lost_ack", "unknown"]
)
def test_mutation_commit_failure_and_lost_ack_reconciliation(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    database = privacy_db[0]
    identifier = create(client, database, "mixed")
    now = terminal(database, identifier) + timedelta(days=181)
    command = prepare(database, identifier, now)
    tmp_path.chmod(0o700)
    sink = SyntheticJournal(tmp_path)
    original, calls = database.unit_of_work, 0
    with original() as session:
        before = graph(session)

    @contextmanager
    def interrupted() -> Iterator[Session]:
        nonlocal calls
        calls += 1
        this_call = calls
        if failure == "unknown" and this_call >= 3:
            raise OSError("synthetic database partition")
        with original() as session:
            yield session
            if this_call == 2 and failure == "rollback":
                raise OSError("synthetic crash before commit")
        if this_call == 2 and failure in {"lost_ack", "unknown"}:
            raise OSError("synthetic lost commit acknowledgement")

    def failed_commit(connection: Connection) -> None:
        # Fail only the mutation commit, after its full flush.
        if calls == 2:
            raise DBAPIError("synthetic commit", None, RuntimeError("synthetic"))

    monkeypatch.setattr(database, "unit_of_work", interrupted)
    if failure == "commit_failure":
        event.listen(database.engine, "commit", failed_commit)
    try:
        result = reconcile_command(database, command, sink, execute=True, now=now)
    finally:
        monkeypatch.setattr(database, "unit_of_work", original)
        if failure == "commit_failure":
            event.remove(database.engine, "commit", failed_commit)
    assert (
        result.state
        == {
            "rollback": JournalState.WITNESSED,
            "commit_failure": JournalState.WITNESSED,
            "lost_ack": JournalState.APPLIED,
            "unknown": JournalState.UNCONFIRMED,
        }[failure]
    )
    assert result.commit_confirmed == (failure == "lost_ack")
    if failure in {"rollback", "commit_failure"}:
        with original() as session:
            assert graph(session) == before
            entry = session.get(DeletionEntry, command.id)
            assert entry is not None and entry.applied_at is None
    assert reconcile_command(
        database, command, sink, execute=True, now=now
    ).commit_confirmed


@pytest.mark.parametrize(
    "damage",
    ["unavailable", "partial", "mismatch", "missing_after_apply", "receipt_only"],
)
def test_sink_failure_never_claims_completion(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    damage: str,
) -> None:
    database = privacy_db[0]
    identifier = create(client, database)
    now = terminal(database, identifier) + timedelta(days=181)
    command = prepare(database, identifier, now)
    tmp_path.chmod(0o700)
    sink = SyntheticJournal(tmp_path)
    path = tmp_path / (str(command.id) + ".json")
    if damage == "missing_after_apply":
        assert reconcile_command(
            database, command, sink, execute=True, now=now
        ).commit_confirmed
        # Fault injection: the API itself has no prune capability.
        path.unlink()
    elif damage in {"partial", "mismatch"}:
        path.write_bytes(
            command.canonical()[:20]
            if damage == "partial"
            else command.model_copy(update={"object_id": uuid4()}).canonical()
        )
        path.chmod(0o600)
    if damage == "unavailable":
        with patch.object(sink, "read", side_effect=OSError("synthetic partition")):
            result = reconcile_command(database, command, sink, execute=True, now=now)
    elif damage == "receipt_only":
        from hashlib import sha256

        receipt = DurableAcknowledgement(
            command.id, sha256(command.canonical()).hexdigest()
        )
        with patch.object(sink, "export", return_value=receipt):
            result = reconcile_command(database, command, sink, execute=True, now=now)
    else:
        result = reconcile_command(database, command, sink, execute=True, now=now)
    assert not result.commit_confirmed
    assert (
        result.state
        == {
            "unavailable": JournalState.SINK_UNAVAILABLE,
            "partial": JournalState.CONFLICT,
            "mismatch": JournalState.CONFLICT,
            "missing_after_apply": JournalState.UNCONFIRMED,
            "receipt_only": JournalState.SINK_UNAVAILABLE,
        }[damage]
    )
    if damage != "missing_after_apply":
        with database.unit_of_work() as session:
            row = session.get(Order, identifier)
            assert row is not None and row.pii_erased_at is None


@pytest.mark.parametrize("conflict", ["canonical", "target_uuid", "production"])
def test_reconciliation_rejects_local_conflict_and_production(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conflict: str,
) -> None:
    database = privacy_db[0]
    identifier = create(client, database)
    now = terminal(database, identifier) + timedelta(days=181)
    command = prepare(database, identifier, now)
    tmp_path.chmod(0o700)
    sink = SyntheticJournal(tmp_path)
    sink.export(command, synthetic=True)
    if conflict == "canonical":
        command = command.model_copy(update={"object_id": uuid4()})
    elif conflict == "target_uuid":
        command = command.model_copy(update={"id": uuid4()})
    else:
        monkeypatch.setenv("APP_ENV", "production")
    result = reconcile_command(database, command, sink, execute=True, now=now)
    assert result.state == (
        JournalState.REVIEW_REQUIRED
        if conflict == "production"
        else JournalState.CONFLICT
    )
    assert not result.commit_confirmed
    with database.unit_of_work() as session:
        row = session.get(Order, identifier)
        assert row is not None and row.pii_erased_at is None


@pytest.mark.parametrize("format", [1, 2])
@pytest.mark.parametrize("local_present", [False, True])
@pytest.mark.parametrize("kind", ["support", "commercial", "mixed", "service"])
def test_reapply_older_graph_and_journal_snapshot_without_reset(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    format: int,
    local_present: bool,
    kind: str,
) -> None:
    """Table-level older-state simulation, not a pg_dump/PITR restore drill.

    Keep the newer journal in a separate schema. Install its older table copy
    with all CHECK/FK/triggers and restore the deleted graph's exact synthetic
    rows. No reset, downgrade, stamp, disabled trigger or volume removal.
    """
    runtime, migrator = privacy_db
    identifier = create(client, runtime, kind)
    sibling = create(client, runtime, "support")
    tmp_path.chmod(0o700)
    sink = SyntheticJournal(tmp_path)
    archive = "synthetic_restore_" + uuid4().hex
    with migrator.unit_of_work() as session:
        assert (
            session.scalar(
                text(
                    "SELECT shobj_description(oid,'pg_database') FROM pg_database "
                    "WHERE datname=current_database()"
                )
            )
            == "akbrizcase:synthetic-seed:test"
        )
        session.execute(text(f"CREATE SCHEMA {archive}"))
        row = session.get(Order, identifier)
        assert row is not None
        consent_id = row.consent_id
        before = graph(session)
        snapshots = {
            name: [
                dict(row)
                for row in session.execute(
                    select(Base.metadata.tables["app." + name])
                ).mappings()
            ]
            for name in GRAPH_TABLES
        }
        if not local_present:
            session.execute(
                text(
                    f"CREATE TABLE {archive}.older_journal "
                    "(LIKE app.deletion_journal INCLUDING ALL)"
                )
            )
        if format == 1:
            at = utc_now()
            entry = DeletionEntry(
                format=1,
                operation="order_delete",
                object_id=identifier,
                policy_version=demo_bundle().consent.version,
                created_at=at,
                retain_until=at
                + timedelta(days=demo_bundle().retention.deletion_journal_days),
                consent_id=consent_id,
                decision_id=uuid4(),
            )
            session.add(entry)
            session.flush()
        else:
            entry = prepare_entry(
                session,
                "order_delete",
                identifier,
                demo_bundle(),
                utc_now(),
                consent_id=consent_id,
                decision_id=uuid4(),
            )
        command = command_for(entry)
        if local_present:
            session.execute(
                text(
                    f"CREATE TABLE {archive}.older_journal "
                    "(LIKE app.deletion_journal INCLUDING ALL)"
                )
            )
            session.execute(
                text(
                    f"INSERT INTO {archive}.older_journal "
                    "SELECT * FROM app.deletion_journal"
                )
            )
        foreign_keys = list(
            session.scalars(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='app.deletion_journal'::regclass AND contype='f'"
                )
            )
        )
        triggers = list(
            session.scalars(
                text(
                    "SELECT pg_get_triggerdef(oid) FROM pg_trigger "
                    "WHERE tgrelid='app.deletion_journal'::regclass "
                    "AND NOT tgisinternal"
                )
            )
        )
    assert reconcile_command(
        migrator,
        command,
        sink,
        execute=True,
        authorize_manual=lambda s: authorize(s, "privacy-admin", PASSWORD) == "SUCCESS",
    ).commit_confirmed
    # Exact graph rows are now absent; INSERT restores the old synthetic snapshot.
    with migrator.unit_of_work() as session:
        assert session.get(Order, identifier) is None
        session.execute(text(f"ALTER TABLE app.deletion_journal SET SCHEMA {archive}"))
        session.execute(text(f"ALTER TABLE {archive}.older_journal SET SCHEMA app"))
        session.execute(
            text("ALTER TABLE app.older_journal RENAME TO deletion_journal")
        )
        for definition in foreign_keys:
            session.execute(text("ALTER TABLE app.deletion_journal ADD " + definition))
        for definition in triggers:
            session.execute(text(definition))
        for name in (
            "consents",
            "orders",
            "order_items",
            "order_status_history",
            "email_outbox",
        ):
            table = Base.metadata.tables["app." + name]
            for values in snapshots[name]:
                if values.get("id") not in set(session.scalars(select(table.c.id))):
                    session.execute(insert(table).values(values))
        assert graph(session) == before
    try:
        result = reconcile_command(migrator, command, sink)
        assert result.state == (
            JournalState.WITNESSED if local_present else JournalState.LOCAL_MISSING
        )
        result = reconcile_command(
            migrator, command, sink, execute=True, restore_missing=True
        )
        assert result.state == JournalState.REVIEW_REQUIRED
        result = reconcile_command(
            migrator,
            command,
            sink,
            execute=True,
            restore_missing=True,
            authorize_manual=lambda s: (
                authorize(s, "privacy-admin", PASSWORD) == "SUCCESS"
            ),
        )
        assert result.state == JournalState.APPLIED and result.commit_confirmed
        with migrator.unit_of_work() as session:
            assert session.get(Order, identifier) is None
            assert session.get(Order, sibling) is not None
            restored_entry = session.get(DeletionEntry, command.id)
            assert (
                restored_entry is not None
                and command_for(restored_entry).canonical() == command.canonical()
            )
        assert sink.read(command.id, synthetic=True) == command.canonical()
    finally:
        # Put the unchanged newer original back. Dispose only this test's table
        # copy so later Alembic/model checks do not see synthetic archive objects.
        with migrator.unit_of_work() as session:
            session.execute(
                text("ALTER TABLE app.deletion_journal RENAME TO reapplied_journal")
            )
            session.execute(
                text(f"ALTER TABLE app.reapplied_journal SET SCHEMA {archive}")
            )
            session.execute(
                text(f"ALTER TABLE {archive}.deletion_journal SET SCHEMA app")
            )
            session.execute(text(f"DROP TABLE {archive}.reapplied_journal"))
            session.execute(text(f"DROP SCHEMA {archive}"))
