"""Closed synthetic restore. A READY result authorizes manual review only.

The trusted controller supplies a freshly claimed PG16 instance and file volume
on an internal network. This module never starts API, SMTP, workers or ingress.
"""

import os
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import select, text

from app.backup.format import MAX_ARCHIVE, BackupBlocked, Manifest, unseal
from app.backup.journal import Checkpoint, CompleteSyntheticJournal, replay
from app.backup.snapshot import PgTools, referenced_files
from app.core.db import SCHEMA_REVISION, Database
from app.modules.orders.erasure_service import pii_erasure_complete
from app.modules.orders.models import Order
from app.modules.orders.privacy_service import synthetic_database


def closed_environment() -> None:
    if (
        os.environ.get("APP_ENV") != "test"
        or os.environ.get("BACKUP_RESTORE_ISOLATED") != "1"
        or any(v for k, v in os.environ.items() if k.startswith("SMTP_"))
    ):
        raise BackupBlocked("RESTORE_ISOLATION_REQUIRED")


def empty_target(database: Database, root: Path) -> None:
    from app.modules.kb.files.storage import open_root

    closed_environment()
    fd = open_root(root)
    try:
        if os.listdir(fd):
            raise BackupBlocked("RESTORE_VOLUME_NOT_EMPTY")
    finally:
        os.close(fd)
    with database.unit_of_work() as session:
        if (
            not synthetic_database(session)
            or session.scalar(text("SELECT current_user")) != "akbrizcase_migrator"
            or int(session.scalar(text("SHOW server_version_num"))) // 10000 != 16
            or session.scalar(text("SELECT to_regnamespace('app')")) is not None
            or session.scalar(
                text(
                    "SELECT count(*) FROM pg_tables WHERE "
                    "schemaname NOT IN ('pg_catalog','information_schema')"
                )
            )
            != 0
        ):
            raise BackupBlocked("RESTORE_DATABASE_NOT_EMPTY")


def invariants(database: Database, sequence_floor: int = 0) -> dict[str, int | bool]:
    with database.unit_of_work() as session:
        if set(
            session.scalars(text("SELECT version_num FROM app.alembic_version"))
        ) != {SCHEMA_REVISION}:
            raise BackupBlocked("SCHEMA_INCOMPATIBLE")
        quote = database.engine.dialect.identifier_preparer.quote_identifier

        def table(name: str) -> str:
            return ".".join(quote(p) for p in name.split("."))

        # MVP-038 deliberately added an existing CHECK as NOT VALID to preserve
        # upgrade compatibility. Validate actual restored rows, rather than
        # mistaking this historical flag for proof of corrupt data.
        for relation, name in session.execute(
            text(
                "SELECT c.conrelid::regclass::text,c.conname FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='app' "
                "AND NOT c.convalidated AND c.contype IN ('c','f')"
            )
        ):
            session.execute(
                text(f"ALTER TABLE {table(relation)} VALIDATE CONSTRAINT {quote(name)}")
            )
        # Recheck every FK against restored rows; no row values leave the process.
        constraints = session.execute(
            text("""
            SELECT c.conrelid::regclass::text, c.confrelid::regclass::text,
              array(SELECT attname FROM unnest(c.conkey) WITH ORDINALITY k(num,ord)
                    JOIN pg_attribute a ON a.attrelid=c.conrelid
                      AND a.attnum=k.num ORDER BY ord),
              array(SELECT attname FROM unnest(c.confkey) WITH ORDINALITY k(num,ord)
                    JOIN pg_attribute a ON a.attrelid=c.confrelid
                      AND a.attnum=k.num ORDER BY ord)
            FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
            WHERE n.nspname='app' AND c.contype='f'
        """)
        ).all()
        for child, parent, local, remote in constraints:
            # Table regclass names come only from our catalog. Quote each segment.
            present = " AND ".join("c." + quote(c) + " IS NOT NULL" for c in local)
            match = " AND ".join(
                "c." + quote(c) + "=p." + quote(p)
                for c, p in zip(local, remote, strict=True)
            )
            if session.scalar(
                text(
                    f"SELECT EXISTS (SELECT 1 FROM {table(child)} c "
                    f"WHERE {present} AND NOT EXISTS "
                    f"(SELECT 1 FROM {table(parent)} p WHERE {match}))"
                )
            ):
                raise BackupBlocked("FK_INVALID")
        for identifier in session.scalars(
            select(Order.id).where(Order.pii_erased_at.is_not(None))
        ):
            if not pii_erasure_complete(session, identifier):
                raise BackupBlocked("ERASURE_INVALID")
        maximum = session.scalar(text("SELECT coalesce(max(number),0) FROM app.orders"))
        next_number = session.scalar(text("SELECT nextval('app.order_number_seq')"))
        if next_number <= max(maximum, sequence_floor):
            raise BackupBlocked("SEQUENCE_INVALID")
        return {"fk_checked": len(constraints), "constraints": True, "sequence": True}


def restore(
    database: Database,
    tools: PgTools,
    root: Path,
    ciphertext: bytes,
    *,
    identity: str,
    application_head: str,
    image_digest: str,
    journal: CompleteSyntheticJournal,
    checkpoint: Checkpoint,
    login: str,
    password: str,
) -> tuple[Manifest, dict[str, int | bool]]:
    empty_target(database, root)
    manifest, dump, files = unseal(ciphertext, identity)
    if (
        manifest.schema_revision != SCHEMA_REVISION
        or manifest.application_head != application_head
        or manifest.image_digest != image_digest
    ):
        raise BackupBlocked("APPLICATION_INCOMPATIBLE")
    # Authenticate using the verified dump only AFTER integrity/version preflight.
    # The host operator is already authorized by the protected private controller;
    # restored active admin+migrator authority is rechecked before every manual
    # deletion, using the existing MVP-038 seam. Never borrow production secrets.
    staged = Path(tempfile.mkdtemp(prefix=".restore-", dir=root))
    try:
        with tempfile.TemporaryDirectory(prefix="akbriz-restore-") as temporary:
            path = Path(temporary) / "database.dump"
            path.write_bytes(dump)
            path.chmod(0o600)
            if path.stat().st_size > MAX_ARCHIVE:
                raise BackupBlocked("ARCHIVE_LIMIT")
            tools.run(
                "pg_restore",
                "--exit-on-error",
                "--single-transaction",
                "--no-password",
                "--dbname=" + tools.settings.database,
                str(path),
            )
        for entry in manifest.files:
            target = staged / entry.path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # mkdir(parents=True) uses umask for intermediate directories.
            target.parent.parent.chmod(0o700)
            fd = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(files[entry.path])
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), entry.mode)
        with database.unit_of_work() as session:
            from app.core.auth import has_role
            from app.core.staff import authenticate_operator

            actor = authenticate_operator(session, login, password)
            if actor is None or not has_role(actor.user, "admin"):
                raise BackupBlocked("OPERATOR_DENIED")
            if referenced_files(session, staged) != files:
                raise BackupBlocked("FILE_SET_INVALID")
        before = invariants(database, manifest.sequence_floor)
        # This fence covers final checks, so no command can be exported during
        # the acceptance window. New journal era/hold/policy uncertainty blocks.
        with journal.fenced(checkpoint) as commands:
            count = replay(database, journal, commands, login=login, password=password)
            result = invariants(database, manifest.sequence_floor)
            with database.unit_of_work() as session:
                if referenced_files(session, staged) != files:
                    raise BackupBlocked("FILE_SET_INVALID")
            for area in ("documents", "images"):
                if (staged / area).exists():
                    os.rename(staged / area, root / area)
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return manifest, {
                **before,
                **result,
                "deletion_replayed": count,
                "files_checked": len(files),
                "completeness_synthetic": True,
            }
    except BaseException:
        # Only our initially empty, privately claimed volume is touched.
        for area in ("documents", "images"):
            if (root / area).is_dir():
                shutil.rmtree(root / area)
        raise
    finally:
        shutil.rmtree(staged)
