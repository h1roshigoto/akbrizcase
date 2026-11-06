"""Dedicated PG16 encrypted backup -> new PG16/files drill, wholly synthetic."""

import io
import json
import os
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import uuid4

import pyrage  # type: ignore[import-untyped]
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from test_catalog_image_binding import picture
from test_cleanup import create
from test_kb_import import SyntheticClamAV
from test_kb_lifecycle import draft, operation, review_publish
from test_privacy import PASSWORD

from app.backup.format import BackupBlocked, seal, unseal
from app.backup.journal import CompleteSyntheticJournal, replay
from app.backup.restore import empty_target, restore
from app.backup.snapshot import PgTools, capture, deletion_lease
from app.backup.storage import SyntheticStorage, publish_verified
from app.core.db import SCHEMA_REVISION, Database, DatabaseSettings
from app.core.db_types import utc_now
from app.core.staff import bootstrap_admin
from app.main import app
from app.modules.catalog.images.pipeline import ImagePipeline
from app.modules.catalog.models import CatalogItem
from app.modules.catalog.router import image_pipeline
from app.modules.chat.models import ChatMessage, ChatSession
from app.modules.kb.files import FilePipeline
from app.modules.kb.models import Document
from app.modules.kb.router import file_pipeline
from app.modules.notify.models import EmailOutbox
from app.modules.notify.smtp import SMTPSettings
from app.modules.orders.cleanup_service import eligibility, prepare_entry
from app.modules.orders.deletion_journal import command_for
from app.modules.orders.deletion_operator import authorize, prepare_deletion
from app.modules.orders.deletion_reconciliation import JournalState, reconcile_command
from app.modules.orders.erasure_service import pii_erasure_complete
from app.modules.orders.models import Consent, Order, OrderItem, OrderStatusHistory
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


def test_encrypted_clean_restore(
    databases: tuple[Database, Database], tmp_path: Path
) -> None:
    runtime, source = databases
    seed_consent(runtime, mode="test")
    with source.unit_of_work() as session:
        assert bootstrap_admin(session, login=LOGIN, password=PASSWORD).ok
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
    with pytest.raises(FileNotFoundError):
        capture(source, PgTools(source_settings), root, **common)
    file.with_suffix(".missing").rename(file)
    file.chmod(0o600)
    file.write_bytes(b"corrupt")
    file.chmod(0o400)
    with pytest.raises(ValueError):
        capture(source, PgTools(source_settings), root, **common)
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
            capture(source, PgTools(source_settings), root, **common)
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
            source, ConcurrentDump(source_settings), root, **common
        )
        for future in futures:
            future.result(timeout=5)
    backup_seconds = monotonic() - backup_start
    assert finished.is_set()
    decoded, dump_bytes, files = unseal(ciphertext, str(identity))
    assert decoded == manifest and len(files) == 10
    assert not any(
        "quarantine" in path or replacement.image_key in path for path in files
    )
    store = SyntheticStorage(Path("/var/lib/akbrizcase/store"))
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
    journal = CompleteSyntheticJournal(Path("/var/lib/akbrizcase/journal"), uuid4())
    commands = []
    for identifier in deleted:
        with source.unit_of_work() as session:
            row = session.get(Order, identifier)
            assert row is not None
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
        port=5432,
        database="akbrizcase",
        user="akbrizcase_migrator",
        password=Path("/run/secrets/restore_migrator_password").read_text().strip(),
    )
    target = Database(target_settings)
    target_files = Path("/var/lib/akbrizcase/restore")
    try:
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
            )
        empty_target(target, target_files)
        (target_files / "foreign").write_bytes(b"synthetic")
        with pytest.raises(BackupBlocked):
            empty_target(target, target_files)
        (target_files / "foreign").unlink()
        restore_start = monotonic()
        _, result = restore(
            target,
            PgTools(target_settings),
            target_files,
            store.read(backup_id),
            identity=str(identity),
            application_head=head,
            image_digest=image,
            journal=journal,
            checkpoint=checkpoint,
            login=LOGIN,
            password=PASSWORD,
        )
        restore_seconds = monotonic() - restore_start
        assert result["deletion_replayed"] == 6
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
                port=5432,
                database="akbrizcase",
                user="akbrizcase_runtime",
                password=Path("/run/secrets/restore_runtime_password")
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
        finally:
            app.dependency_overrides.clear()
            target_runtime.close()
        with pytest.raises(BackupBlocked):
            empty_target(target, target_files)
        rpo_seconds = (datetime.now(UTC) - manifest.snapshot_at).total_seconds()
        print(
            "BACKUP_EVIDENCE "
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
                    "offsite": "BLOCKED",
                    "target_rpo_rto": "BLOCKED",
                },
                sort_keys=True,
            )
        )
    finally:
        target.close()
