"""One exported PG16 snapshot for pg_dump AND the referenced file inventory.

Lock order: filesystem shared deletion lease -> try staff advisory -> NOWAIT
SHARE catalog_items/documents/document_versions (fixed order). No row/content/
cleanup locks are acquired. Any table-lock contention aborts the whole attempt.
Immutable file creation needs no lease; physical removal requires exclusive lease.
"""

import fcntl
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.backup.format import (
    MAX_ARCHIVE,
    BackupBlocked,
    FileEntry,
    Manifest,
    digest,
    private_read,
    seal,
)
from app.core.auth import has_role
from app.core.db import SCHEMA_REVISION, Database, DatabaseSettings
from app.core.staff import authenticate_operator
from app.modules.catalog.images.pipeline import ImagePipeline
from app.modules.catalog.models import CatalogItem
from app.modules.kb.files import storage
from app.modules.kb.models import DocumentVersion


@contextmanager
def deletion_lease(root: Path, *, removal: bool = False) -> Iterator[None]:
    directory = storage.open_root(root)
    fd = -1
    try:
        fd = os.open(
            ".backup-lease",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise BackupBlocked("FILE_LEASE")
        fcntl.flock(fd, (fcntl.LOCK_EX if removal else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        yield
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(directory)


class PgTools:
    """Trusted local executables, bounded runtime, private pgpass, captured errors."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings

    def run(self, command: str, *arguments: str) -> None:
        if command not in {"pg_dump", "pg_restore"}:
            raise BackupBlocked("PG_TOOL")
        with tempfile.TemporaryDirectory(prefix="akbriz-pg-") as temporary:
            password = Path(temporary) / "pgpass"
            values = (
                self.settings.host,
                str(self.settings.port),
                self.settings.database,
                self.settings.user,
                self.settings.password,
            )
            if any("\n" in v or "\r" in v for v in values):
                raise BackupBlocked("PG_CREDENTIAL")
            password.write_text(
                ":".join(v.replace("\\", "\\\\").replace(":", "\\:") for v in values)
                + "\n"
            )
            password.chmod(0o600)
            environment = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PGHOST": self.settings.host,
                "PGPORT": str(self.settings.port),
                "PGDATABASE": self.settings.database,
                "PGUSER": self.settings.user,
                "PGPASSFILE": str(password),
                "PGCONNECT_TIMEOUT": "5",
                "PGOPTIONS": "-c search_path=app,public -c statement_timeout=120000 "
                "-c lock_timeout=2000",
            }
            subprocess.run(
                [command, *arguments],
                env=environment,
                capture_output=True,
                check=True,
                timeout=120,
            )


def referenced_files(session: Session, root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    # Include every bound document version, including internal/retired/draft.
    # This preserves references without promoting quarantine/unbound uploads.
    for version in session.scalars(
        select(DocumentVersion).order_by(DocumentVersion.id)
    ):
        storage.valid_key(version.file_key)
        base = "documents/verified/" + version.file_key
        data = private_read(root / base, storage.MAX_BYTES, mode=0o400)
        receipt = private_read(
            root / "documents/receipts" / version.file_key, 4096, mode=0o400
        )
        evidence = json.loads(receipt)
        if (
            len(data) != version.size_bytes
            or digest(data) != version.checksum_sha256
            or evidence["file_key"] != version.file_key
            or evidence["mime"] != version.mime
            or evidence["size_bytes"] != version.size_bytes
            or evidence["checksum_sha256"] != version.checksum_sha256
        ):
            raise BackupBlocked("DOCUMENT_INTEGRITY")
        result[base] = data
        result["documents/receipts/" + version.file_key] = receipt
        if len(result) > 10000 or sum(map(len, result.values())) > MAX_ARCHIVE:
            raise BackupBlocked("ARCHIVE_LIMIT")
    pipeline = ImagePipeline(root)
    keys = session.scalars(
        select(CatalogItem.image_key)
        .where(CatalogItem.image_key.is_not(None))
        .distinct()
    )
    for key in keys:
        assert key is not None
        pipeline.metadata(key)  # Receipt, signature, checksum and strict storage ACL.
        result["images/verified/" + key] = private_read(
            root / "images/verified" / key, 5 * 1024**2, mode=0o400
        )
        result["images/receipts/" + key] = private_read(
            root / "images/receipts" / key, 4096, mode=0o400
        )
    if len(result) > 10000 or sum(map(len, result.values())) > MAX_ARCHIVE:
        raise BackupBlocked("ARCHIVE_LIMIT")
    return result


def capture(
    database: Database,
    tools: PgTools,
    root: Path,
    *,
    login: str,
    password: str,
    application_head: str,
    image_digest: str,
    recipient: str,
) -> tuple[Manifest, bytes]:
    with (
        deletion_lease(root),
        tempfile.TemporaryDirectory(prefix="akbriz-dump-") as temp,
    ):
        dump_path = Path(temp) / "database.dump"
        with database.unit_of_work() as session:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            session.execute(text("SET LOCAL statement_timeout = '120s'"))
            session.execute(
                text("SET LOCAL idle_in_transaction_session_timeout = '130s'")
            )
            if not session.scalar(text("SELECT pg_try_advisory_xact_lock(4004)")):
                raise BackupBlocked("SNAPSHOT_BUSY")
            session.execute(
                text(
                    "LOCK TABLE app.catalog_items, app.documents, "
                    "app.document_versions "
                    "IN SHARE MODE NOWAIT"
                )
            )
            actor = authenticate_operator(session, login, password)
            if (
                actor is None
                or not has_role(actor.user, "admin")
                or session.scalar(text("SELECT current_user")) != "akbrizcase_migrator"
            ):
                raise BackupBlocked("OPERATOR_DENIED")
            if int(
                session.scalar(text("SHOW server_version_num"))
            ) // 10000 != 16 or set(
                session.scalars(text("SELECT version_num FROM app.alembic_version"))
            ) != {SCHEMA_REVISION}:
                raise BackupBlocked("SCHEMA_INCOMPATIBLE")
            snapshot = session.scalar(text("SELECT pg_export_snapshot()"))
            at = session.scalar(text("SELECT transaction_timestamp()"))
            assert isinstance(snapshot, str) and isinstance(at, datetime)
            files = referenced_files(session, root)
            sequence_floor = session.scalar(
                text(
                    "SELECT CASE WHEN is_called THEN last_value ELSE last_value-1 END "
                    "FROM app.order_number_seq"
                )
            )
            tools.run(
                "pg_dump",
                "--format=custom",
                "--schema=app",
                "--no-password",
                "--snapshot=" + snapshot,
                "--file=" + str(dump_path),
            )
            dump_path.chmod(0o600)
            dump = private_read(dump_path, MAX_ARCHIVE)
            # Reopen sources before releasing guards: catch replacement, truncation
            # and administrator interference, even outside the supported protocol.
            if referenced_files(session, root) != files:
                raise BackupBlocked("FILE_CHANGED")
            manifest = Manifest(
                schema_revision=SCHEMA_REVISION,
                application_head=application_head,
                image_digest=image_digest,
                snapshot_at=at,
                sequence_floor=sequence_floor,
                dump_sha256=digest(dump),
                dump_size=len(dump),
                files=tuple(
                    FileEntry(path=p, sha256=digest(b), size=len(b))
                    for p, b in sorted(files.items())
                ),
            )
        # No locks/transaction across encryption or remote storage I/O.
        return manifest, seal(manifest, dump, files, recipient)
