"""Dedicated PG16 encrypted backup -> new PG16/files drill, wholly synthetic."""

import hmac
import io
import json
import os
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from time import monotonic
from typing import cast
from uuid import uuid4

import pyrage  # type: ignore[import-untyped]
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from test_catalog_image_binding import picture
from test_cleanup import create
from test_kb_import import SyntheticClamAV
from test_kb_lifecycle import draft, operation, review_publish
from test_privacy import PASSWORD

from app.backup.format import BackupBlocked, canonical, seal, unseal
from app.backup.high_water import (
    HighWaterCheckpoint,
    SyntheticSequenceAuthority,
    VerifiedHighWater,
)
from app.backup.journal import CompleteSyntheticJournal, replay
from app.backup.restore import empty_target, invariants, restore
from app.backup.snapshot import PgTools, capture, deletion_lease
from app.backup.storage import SyntheticStorage, atomic_write, publish_verified
from app.core.csrf import SESSION_COOKIE
from app.core.db import SCHEMA_REVISION, Database, DatabaseSettings
from app.core.db_types import utc_now
from app.core.staff import bootstrap_admin
from app.core.staff_models import StaffSession
from app.core.staff_sessions import token_hash
from app.main import app
from app.modules.catalog.images.pipeline import ImagePipeline
from app.modules.catalog.models import CatalogItem
from app.modules.catalog.router import image_pipeline
from app.modules.chat.models import ChatMessage, ChatSession
from app.modules.kb.files import FilePipeline, FileRejected
from app.modules.kb.models import Document
from app.modules.kb.router import file_pipeline
from app.modules.notify.models import EmailOutbox
from app.modules.notify.smtp import SMTPSettings
from app.modules.orders.cleanup_service import eligibility, prepare_entry
from app.modules.orders.deletion_journal import command_for
from app.modules.orders.deletion_models import DeletionEntry
from app.modules.orders.deletion_operator import authorize, prepare_deletion
from app.modules.orders.deletion_reconciliation import JournalState, reconcile_command
from app.modules.orders.erasure_service import pii_erasure_complete
from app.modules.orders.models import (
    Consent,
    ConsentText,
    Order,
    OrderItem,
    OrderStatusHistory,
)
from app.modules.orders.security import OrderSecurity
from app.modules.orders.seed import DEMO_VERSION, seed_consent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("BACKUP_RESTORE_ISOLATED") != "1",
        reason="Dedicated backup job supplies a second fresh PG16 instance",
    ),
]
LOGIN = "backup-admin"


@pytest.fixture
def source_fences() -> Iterator[ExitStack]:
    with ExitStack() as fences:
        yield fences


def test_encrypted_clean_restore(
    databases: tuple[Database, Database], tmp_path: Path, source_fences: ExitStack
) -> None:
    runtime, source = databases
    with source.unit_of_work() as session:
        session.execute(update(ConsentText).values(retired_at=utc_now()))
    seed_consent(runtime, mode="test")
    with source.unit_of_work() as session:
        session.execute(
            update(ConsentText)
            .where(ConsentText.version == DEMO_VERSION)
            .values(retired_at=None)
        )
    with source.unit_of_work() as session:
        admin_result = bootstrap_admin(session, login=LOGIN, password=PASSWORD)
        assert admin_result.ok
        session.add(
            StaffSession(
                user_id=admin_result.user_id,
                token_hash=token_hash("b" * 64),
                expires_at=utc_now() + timedelta(hours=1),
            )
        )
    root = Path(os.environ["APP_FILES_DIR"])
    signatures = tmp_path / "signatures"
    signatures.mkdir()
    (signatures / "test.ndb").write_text(
        "Synthetic.Backup.Test:0:*:53594e5448494e464543544544\n"
    )
    scanner = SyntheticClamAV(signatures)
    documents = FilePipeline(root, _scanner=scanner)
    images = ImagePipeline(root, _scanner=scanner)
    ids = []
    for index, kind in enumerate(
        ("support", "commercial", "mixed", "service", "mixed", "support", "support")
    ):
        with TestClient(app, client=(f"192.0.2.{index + 1}", 50000)) as client:
            app.state.database = runtime
            app.state.order_security = OrderSecurity(b"synthetic-privacy-key" * 3)
            ids.append(create(client, runtime, kind))
    deleted, kept, pii, support = ids[:4], ids[4], ids[5], ids[6]
    with source.unit_of_work() as session:
        row = session.get(Order, pii)
        assert row is not None
        row.created_at = utc_now() - timedelta(days=200)
        row.status, row.terminal_at, row.closed_at = (
            "closed",
            utc_now() - timedelta(days=190),
            utc_now() - timedelta(days=190),
        )
        session.add(
            OrderStatusHistory(
                order_id=pii, to_status="closed", reason="Synthetic private reason"
            )
        )
        chat = ChatSession(
            owner_token_hash="e" * 64,
            mode="knowledge",
            retention_version=DEMO_VERSION,
            created_at=utc_now() - timedelta(days=31),
            last_activity_at=utc_now() - timedelta(days=31),
            expires_at=utc_now() - timedelta(days=30),
        )
        # Exact 24h CHECK uses one shared instant.
        chat.last_activity_at, chat.expires_at = (
            chat.created_at,
            chat.created_at + timedelta(hours=24),
        )
        session.add(chat)
        session.flush()
        session.add(
            ChatMessage(
                session_id=chat.id,
                request_id=uuid4(),
                request_hash="e" * 64,
                input={"text": "Synthetic private chat"},
                response={"text": "Synthetic answer"},
                state_version_after=1,
                matched_by="none",
                latency_ms=0,
            )
        )
        row = session.get(Order, support)
        assert row is not None
        row.chat_session_id = chat.id
        support_comment, chat_id = row.comment, chat.id
        notification = session.scalar(
            select(EmailOutbox).where(EmailOutbox.order_id == kept)
        )
        assert notification is not None
        notification.status, notification.failed_at = "failed", utc_now()
        item = session.scalar(
            select(CatalogItem).where(CatalogItem.status == "published")
        )
        assert item is not None
        item_id = item.id
    previous = images.accept(
        io.BytesIO(picture(color="gray")), filename="synthetic.png", mime="image/png"
    )
    with source.unit_of_work() as session:
        session.execute(
            update(CatalogItem)
            .where(CatalogItem.id == item_id)
            .values(image_key=previous.image_key)
        )
    accepted = images.accept(
        io.BytesIO(picture()), filename="synthetic.png", mime="image/png"
    )
    replacement = images.accept(
        io.BytesIO(picture(color="blue")), filename="synthetic.png", mime="image/png"
    )
    with source.unit_of_work() as session:
        session.execute(
            update(CatalogItem)
            .where(CatalogItem.id == item_id)
            .values(image_key=accepted.image_key)
        )
    doc_rows = []
    for visibility, status in (
        ("public", "published"),
        ("internal", "published"),
        ("public", "revoked"),
        ("public", "retired"),
    ):
        with source.unit_of_work() as session:
            user, document_id, version_id = draft(
                session, documents, tmp_path, visibility=visibility
            )
            review_publish(
                session,
                documents,
                tmp_path,
                user,
                document_id,
                version_id,
                public=visibility == "public",
            )
            if status in {"revoked", "retired"}:
                assert operation(
                    session,
                    user,
                    documents,
                    "revoke" if status == "revoked" else "retire",
                    document_id,
                    version_id,
                ).ok
            doc_rows.append((document_id, version_id, visibility, status))
    # Quarantine and unbound verified objects must not be mistaken for references.
    stray = root / "documents/quarantine" / ("f" * 64)
    stray.write_bytes(b"Synthetic unfinished private upload")
    stray.chmod(0o400)
    # A previously issued number can outlive its order; max(orders.number)
    # alone must never reset the sequence below that reservation.
    with source.unit_of_work() as session:
        session.scalar(text("SELECT nextval('app.order_number_seq')"))
    key_file = tmp_path / "sequence-key"
    key_file.write_bytes(os.urandom(32))
    key_file.chmod(0o600)
    authority_root = root.parent / "journal" / "sequence"
    authority_root.mkdir(mode=0o700)
    authority = SyntheticSequenceAuthority.create(source, authority_root, key_file)
    source_settings = DatabaseSettings.from_environment(migration=True)
    identity = pyrage.x25519.Identity.generate()
    head, image = os.environ["BACKUP_TEST_HEAD"], os.environ["BACKUP_TEST_IMAGE"]
    common = dict(
        login=LOGIN,
        password=PASSWORD,
        application_head=head,
        image_digest=image,
        recipient=str(identity.to_public()),
    )
    # A missing referenced object aborts before a remotely accepted backup.
    file = root / "images/verified" / accepted.image_key
    saved = file.read_bytes()
    file.rename(file.with_suffix(".missing"))
    with pytest.raises(FileRejected, match="STORAGE_FAILURE"):
        capture(
            source,
            PgTools(source_settings),
            root,
            sequence_source=authority.state().source,
            **common,
        )
    file.with_suffix(".missing").rename(file)
    file.chmod(0o600)
    file.write_bytes(b"corrupt")
    file.chmod(0o400)
    with pytest.raises(ValueError):
        capture(
            source,
            PgTools(source_settings),
            root,
            sequence_source=authority.state().source,
            **common,
        )
    file.chmod(0o600)
    file.write_bytes(saved)
    file.chmod(0o400)
    # Contention before lock acquisition fails immediately and releases staff.
    with source.unit_of_work() as session:
        session.execute(
            update(CatalogItem)
            .where(CatalogItem.id == item_id)
            .values(name="Synthetic renamed")
        )
        with pytest.raises(DBAPIError):
            capture(
                source,
                PgTools(source_settings),
                root,
                sequence_source=authority.state().source,
                **common,
            )
    started, finished = Event(), Event()

    def change_image() -> None:
        with source.unit_of_work() as session:
            started.set()
            session.execute(
                update(CatalogItem)
                .where(CatalogItem.id == item_id)
                .values(image_key=replacement.image_key)
            )
        finished.set()

    class ConcurrentDump(PgTools):
        def run(self, command: str, *args: str) -> None:
            futures.append(pool.submit(change_image))
            assert started.wait(3)
            assert not finished.wait(0.15)
            # Both lifecycle table mutations and physical removal are excluded.
            with source.unit_of_work() as session:
                session.execute(text("SET LOCAL lock_timeout='100ms'"))
                with pytest.raises(DBAPIError):
                    session.execute(update(Document).values(visibility="internal"))
            with pytest.raises(BlockingIOError):
                with deletion_lease(root, removal=True):
                    pytest.fail("file removal during snapshot")
            super().run(command, *args)

    backup_start = monotonic()
    futures: list[Future[None]] = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        manifest, ciphertext = capture(
            source,
            ConcurrentDump(source_settings),
            root,
            sequence_source=authority.state().source,
            **common,
        )
        for future in futures:
            future.result(timeout=5)
    backup_seconds = monotonic() - backup_start
    with source.unit_of_work() as session:
        # The bug: this number is absent from both dump and manifest.sequence_floor.
        issued_after_backup = session.scalar(
            text("SELECT nextval('app.order_number_seq')")
        )
        assert issued_after_backup == manifest.sequence_floor + 1
    assert finished.is_set()
    decoded, dump_bytes, files = unseal(ciphertext, str(identity))
    assert decoded == manifest and len(files) == 10
    assert not any(
        "quarantine" in path or replacement.image_key in path for path in files
    )
    store = SyntheticStorage(root.parent / "store")
    backup_id = uuid4()
    publish_verified(store, backup_id, manifest, ciphertext, str(identity))
    # Capture all business rows before post-snapshot deletion, without reporting them.
    with source.unit_of_work() as session:
        business = {
            table: list(
                session.execute(
                    text(f"SELECT * FROM app.{table} WHERE order_id=:id ORDER BY id"),
                    {"id": kept},
                )
            )
            for table in ("order_items", "order_status_history", "email_outbox")
        }
    deletion_root = root.parent / "journal" / "deletions"
    deletion_root.mkdir(mode=0o700)
    journal = CompleteSyntheticJournal(deletion_root, uuid4())
    commands = []
    for offset, identifier in enumerate(deleted):
        with source.unit_of_work() as session:
            row = session.get(Order, identifier)
            assert row is not None
            if offset == 0:
                # Preserve a genuine legacy v1 witness byte-for-byte through
                # actual encrypted backup restore, without rewriting v1 to v2.
                assert authorize(session, LOGIN, PASSWORD) == "SUCCESS"
                at = utc_now()
                legacy = DeletionEntry(
                    format=1,
                    operation="order_delete",
                    object_id=identifier,
                    consent_id=row.consent_id,
                    decision_id=uuid4(),
                    policy_version=DEMO_VERSION,
                    created_at=at,
                    retain_until=at + timedelta(days=14),
                )
                session.add(legacy)
                session.flush()
                commands.append(command_for(legacy))
                continue
            code, cmd = prepare_deletion(
                session,
                login=LOGIN,
                password=PASSWORD,
                order_id=identifier,
                expected_version=row.version,
                decision_id=uuid4(),
                journal_policy_version=DEMO_VERSION,
            )
            assert code == "PREPARED" and cmd is not None
        commands.append(cmd)
    with source.unit_of_work() as session:
        for operation_name, identifier in (
            ("order_pii", pii),
            ("chat_delete", chat_id),
        ):
            candidate = session.get(
                Order if operation_name == "order_pii" else ChatSession, identifier
            )
            assert isinstance(candidate, Order | ChatSession)
            bundle, due, _ = eligibility(session, operation_name, candidate, utc_now())
            assert due
            commands.append(
                command_for(
                    prepare_entry(
                        session, operation_name, identifier, bundle, utc_now()
                    )
                )
            )
    faults = ["before", "after"]

    def mark_apply(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if statement.startswith("UPDATE app.deletion_journal SET applied_at"):
            connection.info["backup_commit_fault"] = True

    def fail_commit(connection: Connection) -> None:
        if not connection.info.pop("backup_commit_fault", False) or not faults:
            return
        mode = faults.pop(0)
        if mode == "after":
            connection.connection.commit()  # Actual server COMMIT, then lost ack.
        raise DBAPIError("SYNTHETIC_COMMIT_ACK", None, RuntimeError("SYNTHETIC"), False)

    event.listen(source.engine, "before_cursor_execute", mark_apply)
    event.listen(source.engine, "commit", fail_commit)
    try:
        uncertain = reconcile_command(
            source,
            commands[0],
            journal,
            execute=True,
            authorize_manual=lambda s: authorize(s, LOGIN, PASSWORD) == "SUCCESS",
        )
        assert (
            uncertain.state == JournalState.WITNESSED and not uncertain.commit_confirmed
        )
        recovered = reconcile_command(
            source,
            commands[0],
            journal,
            execute=True,
            authorize_manual=lambda s: authorize(s, LOGIN, PASSWORD) == "SUCCESS",
        )
        assert recovered.state == JournalState.APPLIED and recovered.commit_confirmed
        assert not faults
    finally:
        event.remove(source.engine, "before_cursor_execute", mark_apply)
        event.remove(source.engine, "commit", fail_commit)
    for cmd in commands:
        reconciliation = reconcile_command(
            source,
            cmd,
            journal,
            execute=True,
            authorize_manual=lambda s: authorize(s, LOGIN, PASSWORD) == "SUCCESS",
        )
        assert (
            reconciliation.state == JournalState.APPLIED
            and reconciliation.commit_confirmed
        )
    checkpoint = journal.checkpoint()  # Independent source-time anchor, AFTER backup.
    target_settings = DatabaseSettings(
        host="restore-postgres",
        port=int(os.environ.get("BACKUP_RESTORE_PORT", "5432")),
        database="akbrizcase",
        user="akbrizcase_migrator",
        password=Path(
            os.environ.get(
                "BACKUP_RESTORE_MIGRATOR_PASSWORD_FILE",
                "/run/secrets/restore_migrator_password",
            )
        )
        .read_text()
        .strip(),
    )
    target = Database(target_settings)
    target_files = root.parent / "restore"
    try:
        high_water = source_fences.enter_context(
            authority.fenced(manifest, login=LOGIN, password=PASSWORD)
        )

        def resign(value: HighWaterCheckpoint) -> HighWaterCheckpoint:
            payload = json.dumps(
                value.model_dump(mode="json", exclude={"mac"}),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            return value.model_copy(
                update={
                    "mac": hmac.digest(key_file.read_bytes(), payload, "sha256").hex()
                }
            )

        point = high_water.checkpoint
        original_checkpoint = canonical(point)
        bad_points = [
            point.model_copy(update={"mac": "0" * 64}),
            resign(
                point.model_copy(
                    update={
                        "source": point.source.model_copy(update={"epoch": uuid4()})
                    }
                )
            ),
            resign(
                point.model_copy(
                    update={"source": point.source.model_copy(update={"id": uuid4()})}
                )
            ),
            resign(
                point.model_copy(update={"issued_at": utc_now() - timedelta(hours=2)})
            ),
            resign(
                point.model_copy(update={"high_water": manifest.sequence_floor - 1})
            ),
            resign(point.model_copy(update={"high_water": manifest.sequence_floor})),
            resign(
                point.model_copy(update={"issued_at": utc_now() + timedelta(minutes=1)})
            ),
            resign(point.model_copy(update={"fence_id": uuid4()})),
        ]
        candidates = [
            None,
            cast(VerifiedHighWater, point),
            *[replace(high_water, checkpoint=p) for p in bad_points],
            replace(high_water, _lease=replace(high_water._lease, active=False)),
        ]
        for water_candidate in candidates:
            if isinstance(water_candidate, VerifiedHighWater):
                # Exercise provenance/epoch/freshness/high-water validation even
                # when the supplied file is canonical and has a valid MAC.
                atomic_write(
                    authority.root,
                    "checkpoint.json",
                    canonical(water_candidate.checkpoint),
                )
            try:
                with pytest.raises(BackupBlocked, match="SEQUENCE_"):
                    restore(
                        target,
                        PgTools(target_settings),
                        target_files,
                        ciphertext,
                        identity=str(identity),
                        application_head=head,
                        image_digest=image,
                        journal=journal,
                        checkpoint=checkpoint,
                        login=LOGIN,
                        password=PASSWORD,
                        high_water=water_candidate,
                    )
            finally:
                atomic_write(authority.root, "checkpoint.json", original_checkpoint)
            empty_target(target, target_files)
        atomic_write(
            authority.root,
            "checkpoint.json",
            canonical(point.model_copy(update={"mac": "0" * 64})),
        )
        with pytest.raises(BackupBlocked, match="SEQUENCE_CHECKPOINT_INVALID"):
            high_water.check(manifest)
        atomic_write(authority.root, "checkpoint.json", original_checkpoint)
        # Missing durable checkpoint cannot be substituted by an in-memory number.
        (authority.root / "checkpoint.json").unlink()
        with pytest.raises(BackupBlocked, match="SEQUENCE_CHECKPOINT_INVALID"):
            high_water.check(manifest)
        atomic_write(authority.root, "checkpoint.json", original_checkpoint)
        # An unbound legacy backup and naked signed JSON cannot authorize writes.
        unknown = manifest.model_copy(update={"sequence_source": None})
        with pytest.raises(BackupBlocked, match="SEQUENCE_"):
            restore(
                target,
                PgTools(target_settings),
                target_files,
                seal(unknown, dump_bytes, files, str(identity.to_public())),
                identity=str(identity),
                application_head=head,
                image_digest=image,
                journal=journal,
                checkpoint=checkpoint,
                login=LOGIN,
                password=PASSWORD,
                high_water=high_water,
            )
        with source.unit_of_work() as other:
            other.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(DBAPIError):
                other.scalar(text("SELECT nextval('app.order_number_seq')"))
        # Preflight failures cannot populate destination or start application code.
        for wrong in (
            {"image_digest": "sha256:" + "0" * 64},
            {"application_head": "0" * 40},
        ):
            with pytest.raises(BackupBlocked):
                restore(
                    target,
                    PgTools(target_settings),
                    target_files,
                    ciphertext,
                    identity=str(identity),
                    application_head=wrong.get("application_head", head),
                    image_digest=wrong.get("image_digest", image),
                    journal=journal,
                    checkpoint=checkpoint,
                    login=LOGIN,
                    password=PASSWORD,
                    high_water=high_water,
                )
            empty_target(target, target_files)
        incompatible = decoded.model_copy(update={"schema_revision": "unknown"})
        with pytest.raises(BackupBlocked):
            restore(
                target,
                PgTools(target_settings),
                target_files,
                seal(incompatible, dump_bytes, files, str(identity.to_public())),
                identity=str(identity),
                application_head=head,
                image_digest=image,
                journal=journal,
                checkpoint=checkpoint,
                login=LOGIN,
                password=PASSWORD,
                high_water=high_water,
            )
        empty_target(target, target_files)
        (target_files / "foreign").write_bytes(b"synthetic")
        with pytest.raises(BackupBlocked):
            empty_target(target, target_files)
        (target_files / "foreign").unlink()
        blocked = Database(
            DatabaseSettings(
                host=target_settings.host,
                port=target_settings.port,
                database="akbrizcase_blocked",
                user=target_settings.user,
                password=target_settings.password,
            )
        )
        try:
            with pytest.raises(BackupBlocked, match="JOURNAL_INCOMPLETE"):
                restore(
                    blocked,
                    PgTools(
                        DatabaseSettings(
                            host=target_settings.host,
                            port=target_settings.port,
                            database="akbrizcase_blocked",
                            user=target_settings.user,
                            password=target_settings.password,
                        )
                    ),
                    root.parent / "blocked",
                    ciphertext,
                    identity=str(identity),
                    application_head=head,
                    image_digest=image,
                    journal=journal,
                    checkpoint=checkpoint.model_copy(
                        update={"count": checkpoint.count + 1}
                    ),
                    login=LOGIN,
                    password=PASSWORD,
                    high_water=high_water,
                )
            assert not list((root.parent / "blocked").iterdir())
            with pytest.raises(BackupBlocked, match="RESTORE_DATABASE_NOT_EMPTY"):
                empty_target(blocked, root.parent / "blocked")
        finally:
            blocked.close()
        restore_start = monotonic()
        _, result = restore(
            target,
            PgTools(target_settings),
            target_files,
            store.read_accepted(backup_id),
            identity=str(identity),
            application_head=head,
            image_digest=image,
            journal=journal,
            checkpoint=checkpoint,
            login=LOGIN,
            password=PASSWORD,
            high_water=high_water,
        )
        restore_seconds = monotonic() - restore_start
        assert result["deletion_replayed"] == 6
        with target.unit_of_work() as session:
            first_restored_number = session.scalar(
                text("SELECT nextval('app.order_number_seq')")
            )
            assert first_restored_number > issued_after_backup
        # Repeated validation includes newly issued target numbers.
        for _ in range(2):
            assert invariants(target, manifest, high_water)["sequence"] is True
        with target.unit_of_work() as session:
            assert (
                session.scalar(text("SELECT nextval('app.order_number_seq')"))
                > first_restored_number
            )
        with pytest.raises(BackupBlocked, match="SEQUENCE_HIGH_WATER_REQUIRED"):
            invariants(target, manifest, None)
        # Idempotent retry checks every witness/postcondition again.
        with journal.fenced(checkpoint) as inventory:
            assert (
                replay(target, journal, inventory, login=LOGIN, password=PASSWORD) == 6
            )
        with target.unit_of_work() as session:
            for identifier in deleted:
                assert session.get(Order, identifier) is None
            assert pii_erasure_complete(session, pii)
            assert session.get(ChatSession, chat_id) is None
            assert (
                session.scalar(
                    select(ChatMessage.id).where(ChatMessage.session_id == chat_id)
                )
                is None
            )
            row = session.get(Order, support)
            assert (
                row is not None
                and row.comment == support_comment
                and row.chat_session_id is None
            )
            for table, before in business.items():
                assert (
                    list(
                        session.execute(
                            text(
                                f"SELECT * FROM app.{table} "
                                "WHERE order_id=:id ORDER BY id"
                            ),
                            {"id": kept},
                        )
                    )
                    == before
                )
            assert (
                session.scalar(
                    select(Consent.id)
                    .join(Order, Order.consent_id == Consent.id)
                    .where(Order.id == kept)
                )
                is not None
            )
            assert (
                session.scalar(
                    select(OrderItem.unit_price_snapshot)
                    .where(OrderItem.order_id == kept)
                    .limit(1)
                )
                is not None
            )
        assert SMTPSettings.from_environment() is None
        target_runtime = Database(
            DatabaseSettings(
                host="restore-postgres",
                port=int(os.environ.get("BACKUP_RESTORE_PORT", "5432")),
                database="akbrizcase",
                user="akbrizcase_runtime",
                password=Path(
                    os.environ.get(
                        "BACKUP_RESTORE_RUNTIME_PASSWORD_FILE",
                        "/run/secrets/restore_runtime_password",
                    )
                )
                .read_text()
                .strip(),
            )
        )
        try:
            assert target_runtime.ready()
            # In-process HTTP assertions expose no listener or network ingress.
            app.dependency_overrides[file_pipeline] = lambda: FilePipeline(
                target_files, _scanner=scanner
            )
            app.dependency_overrides[image_pipeline] = lambda: ImagePipeline(
                target_files, _scanner=scanner
            )
            with TestClient(app) as client:
                app.state.database = target_runtime
                assert (
                    client.get(
                        f"/api/v1/catalog/items/{item_id}/image/{accepted.image_key}"
                    ).status_code
                    == 200
                )
                assert (
                    client.get(
                        f"/api/v1/catalog/items/{item_id}/image/{replacement.image_key}"
                    ).status_code
                    == 404
                )
                for document_id, version_id, visibility, status in doc_rows:
                    response = client.get(
                        f"/api/v1/documents/{document_id}/versions/{version_id}/file"
                    )
                    assert response.status_code == (
                        200 if visibility == "public" and status != "revoked" else 404
                    )
                assert (
                    client.get(
                        f"/api/v1/catalog/items/{item_id}/image/{previous.image_key}"
                    ).status_code
                    == 404
                )
                client.cookies.set(SESSION_COOKIE, "b" * 64)
                for document_id, version_id, _visibility, status in doc_rows:
                    response = client.get(
                        f"/api/v1/documents/{document_id}/versions/{version_id}/file"
                    )
                    assert response.status_code == (404 if status == "revoked" else 200)
                client.cookies.clear()
                with target.unit_of_work() as session:
                    session.execute(
                        update(CatalogItem)
                        .where(CatalogItem.id == item_id)
                        .values(status="archived")
                    )
                assert (
                    client.get(
                        f"/api/v1/catalog/items/{item_id}/image/{accepted.image_key}"
                    ).status_code
                    == 404
                )
        finally:
            app.dependency_overrides.clear()
            target_runtime.close()
        with pytest.raises(BackupBlocked):
            empty_target(target, target_files)
        rpo_seconds = (datetime.now(UTC) - manifest.snapshot_at).total_seconds()
        print(
            "\nBACKUP_EVIDENCE "
            + json.dumps(
                {
                    "head": head,
                    "schema": SCHEMA_REVISION,
                    "backup_seconds": round(backup_seconds, 3),
                    "restore_seconds": round(restore_seconds, 3),
                    "rpo_seconds": round(rpo_seconds, 3),
                    "encrypted_bytes": len(ciphertext),
                    **result,
                    "smtp_disabled": True,
                    "post_snapshot_number_protected": True,
                    "sequence_negative_cases": len(candidates) + 4,
                    "commit_faults_checked": 2,
                    "journal_formats_checked": 2,
                    "incomplete_restore_blocked": True,
                    "offsite": "BLOCKED",
                    "target_rpo_rto": "BLOCKED",
                },
                sort_keys=True,
            )
        )
    finally:
        target.close()
    source_fences.close()
    # Dropping the actual source fence invalidates even an authentic checkpoint.
    expired_target = Database(target_settings)
    try:
        with pytest.raises(BackupBlocked, match="SEQUENCE_SOURCE_NOT_FENCED"):
            invariants(expired_target, manifest, high_water)
    finally:
        expired_target.close()
