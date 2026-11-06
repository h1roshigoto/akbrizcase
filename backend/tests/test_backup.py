"""Real age crypto, storage failures, full-history discovery and private paths."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pyrage  # type: ignore[import-untyped]
import pytest

from app.backup.format import (
    BackupBlocked,
    FileEntry,
    Manifest,
    digest,
    private_read,
    seal,
    unseal,
)
from app.backup.journal import CompleteSyntheticJournal
from app.backup.snapshot import deletion_lease
from app.backup.storage import (
    SyntheticStorage,
    check_age,
    diagnostics_receipt,
    publish_verified,
)
from app.core.db import SCHEMA_REVISION
from app.core.operations import backup_report
from app.modules.orders.deletion_journal import DeletionCommand


@pytest.fixture
def archive() -> tuple[Manifest, bytes, str]:
    identity = pyrage.x25519.Identity.generate()
    path = "documents/verified/" + "a" * 64
    data, dump = b"synthetic private document", b"synthetic SQL dump"
    manifest = Manifest(
        schema_revision=SCHEMA_REVISION,
        application_head="a" * 40,
        image_digest="sha256:" + "b" * 64,
        snapshot_at=datetime.now(UTC),
        dump_sha256=digest(dump),
        dump_size=len(dump),
        files=(FileEntry(path=path, sha256=digest(data), size=len(data)),),
    )
    return (
        manifest,
        seal(manifest, dump, {path: data}, str(identity.to_public())),
        str(identity),
    )


def test_real_age_roundtrip(archive: tuple[Manifest, bytes, str]) -> None:
    manifest, ciphertext, key = archive
    assert ciphertext.startswith(b"age-encryption.org/v1")
    for private in (
        b"synthetic private",
        b"documents/verified",
        SCHEMA_REVISION.encode(),
    ):
        assert private not in ciphertext
    restored, dump, files = unseal(ciphertext, key)
    assert restored == manifest and dump == b"synthetic SQL dump" and len(files) == 1


@pytest.mark.parametrize(
    "fault", ["wrong_key", "ciphertext", "header", "missing_chunk"]
)
def test_authenticated_failure(
    archive: tuple[Manifest, bytes, str], fault: str
) -> None:
    _, ciphertext, key = archive
    if fault == "wrong_key":
        key = str(pyrage.x25519.Identity.generate())
    elif fault == "missing_chunk":
        ciphertext = ciphertext[:-40]
    else:
        offset = 40 if fault == "header" else len(ciphertext) - 10
        ciphertext = (
            ciphertext[:offset]
            + bytes([ciphertext[offset] ^ 1])
            + ciphertext[offset + 1 :]
        )
    with pytest.raises((ValueError, pyrage.DecryptError)):
        unseal(ciphertext, key)


@pytest.mark.parametrize(
    "fault", ["checksum", "size", "missing", "duplicate", "traversal"]
)
def test_manifest_failure(fault: str) -> None:
    data, dump = b"file", b"dump"
    path = "documents/verified/" + "a" * 64
    entry = FileEntry(path=path, sha256=digest(data), size=len(data))
    manifest = Manifest(
        schema_revision=SCHEMA_REVISION,
        application_head="a" * 40,
        image_digest="sha256:" + "b" * 64,
        snapshot_at=datetime.now(UTC),
        dump_sha256=digest(dump),
        dump_size=len(dump),
        files=(entry,),
    )
    if fault == "checksum":
        entry = entry.model_copy(update={"sha256": "0" * 64})
    if fault == "size":
        entry = entry.model_copy(update={"size": 99})
    if fault == "traversal":
        with pytest.raises(ValueError):
            FileEntry(path="../key", sha256=digest(data), size=len(data))
        return
    manifest = manifest.model_copy(
        update={"files": (entry, entry) if fault == "duplicate" else (entry,)}
    )
    with pytest.raises(BackupBlocked):
        seal(
            manifest,
            dump,
            {} if fault == "missing" else {path: data},
            str(pyrage.x25519.Identity.generate().to_public()),
        )


@pytest.mark.parametrize("fault", ["publish", "read", "accept", "corrupt"])
def test_storage_failure(
    tmp_path: Path, archive: tuple[Manifest, bytes, str], fault: str
) -> None:
    tmp_path.chmod(0o700)
    manifest, ciphertext, identity = archive

    class FailingStore(SyntheticStorage):
        def publish(self, backup_id: UUID, data: bytes) -> None:
            if fault == "publish":
                raise OSError
            super().publish(backup_id, data)

        def read(self, backup_id: UUID) -> bytes:
            if fault == "read":
                raise OSError
            data = super().read(backup_id)
            return data[:-1] if fault == "corrupt" else data

        def accept(self, backup_id: UUID, manifest: Manifest, sha256: str) -> None:
            raise OSError

    with pytest.raises((OSError, BackupBlocked)):
        publish_verified(
            FailingStore(tmp_path), uuid4(), manifest, ciphertext, identity
        )
    assert not list(tmp_path.glob("*.json"))
    publish_verified(
        SyntheticStorage(tmp_path), uuid4(), manifest, ciphertext, identity
    )
    assert len(list(tmp_path.glob("*.json"))) == 1
    with pytest.raises(BackupBlocked):
        SyntheticStorage(tmp_path).prune(
            datetime.now(UTC) + timedelta(days=90), approved_days=None
        )
    assert len(list(tmp_path.glob("*.json"))) == 1


def command() -> DeletionCommand:
    now = datetime.now(UTC)
    return DeletionCommand(
        id=uuid4(),
        operation="chat_delete",
        object_id=uuid4(),
        consent_id=None,
        decision_id=None,
        policy_version="SYNTHETIC",
        created_at=now,
        retain_until=now + timedelta(days=14),
    )


@pytest.mark.parametrize(
    "fault", ["omitted", "conflict", "epoch", "new_export", "index"]
)
def test_complete_history(tmp_path: Path, fault: str) -> None:
    tmp_path.chmod(0o700)
    sink = CompleteSyntheticJournal(tmp_path, uuid4())
    first, second = command(), command()
    sink.export(first, synthetic=True)
    sink.export(second, synthetic=True)
    anchor = sink.checkpoint()
    with sink.fenced(anchor) as all_commands:
        assert len(all_commands) == 2
        with pytest.raises(BlockingIOError):
            sink.export(command(), synthetic=True)
    if fault == "omitted":
        (tmp_path / (str(second.id) + ".json")).unlink()
    elif fault == "conflict":
        (tmp_path / (str(second.id) + ".json")).write_bytes(first.canonical())
    elif fault == "epoch":
        anchor = anchor.model_copy(update={"epoch": uuid4()})
    elif fault == "new_export":
        sink.export(command(), synthetic=True)
    else:
        (tmp_path / ".index").unlink()
    with pytest.raises((BackupBlocked, ValueError)):
        with sink.fenced(anchor):
            pytest.fail("incomplete journal accepted")


def test_private_paths_leases_diagnostics(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "identity"
    path.write_text("synthetic")
    path.chmod(0o600)
    assert private_read(path, 4096) == b"synthetic"
    (tmp_path / "link").symlink_to(path)
    with pytest.raises(OSError):
        private_read(tmp_path / "link", 4096)
    with deletion_lease(tmp_path):
        with pytest.raises(BlockingIOError):
            with deletion_lease(tmp_path, removal=True):
                pytest.fail("removal during snapshot")
    receipt = tmp_path / "health.json"
    diagnostics_receipt(receipt, durable_offsite_verified=False)
    assert (
        backup_report(receipt, datetime.now(UTC))["signal"] == "BACKUP_OFFSITE_BLOCKED"
    )
    with pytest.raises(BackupBlocked):
        check_age(datetime.now(UTC) - timedelta(hours=25), datetime.now(UTC))
