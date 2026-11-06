"""Narrow storage contract and local synthetic model. Never an offsite claim."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from app.backup.format import (
    MAX_ARCHIVE,
    BackupBlocked,
    Manifest,
    digest,
    private_read,
    unseal,
)
from app.modules.kb.files.storage import open_root


def atomic_write(root: Path, name: str, data: bytes, *, replace: bool = True) -> None:
    if "/" in name or name in {"", ".", ".."}:
        raise BackupBlocked("STORAGE_NAME")
    directory = open_root(root)
    temporary = "." + uuid4().hex
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            os.link(
                temporary,
                name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


class BackupStorage(Protocol):
    """Trusted adapter. publish is immutable; accept follows durable read-back.

    A production implementation must prove independent RF location, credentials,
    durability, authenticated reads, conditional acceptance and retention policy.
    There is no generic flag that turns the synthetic adapter into that provider.
    """

    def publish(self, backup_id: UUID, ciphertext: bytes) -> None: ...
    def read(self, backup_id: UUID) -> bytes: ...
    def accept(self, backup_id: UUID, manifest: Manifest, sha256: str) -> None: ...


class SyntheticStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def publish(self, backup_id: UUID, ciphertext: bytes) -> None:
        atomic_write(self.root, str(backup_id) + ".age", ciphertext, replace=False)

    def read(self, backup_id: UUID) -> bytes:
        return private_read(
            self.root / (str(backup_id) + ".age"), MAX_ARCHIVE + 1024**2
        )

    def accept(self, backup_id: UUID, manifest: Manifest, sha256: str) -> None:
        if digest(self.read(backup_id)) != sha256:
            raise BackupBlocked("REMOTE_VERIFICATION")
        atomic_write(
            self.root,
            str(backup_id) + ".json",
            json.dumps(
                {
                    "status": "synthetic-verified",
                    "snapshot_at": manifest.snapshot_at.isoformat(),
                    "ciphertext_sha256": sha256,
                },
                sort_keys=True,
            ).encode(),
            replace=False,
        )

    def prune(self, now: datetime, *, approved_days: int | None) -> int:
        # Deliberately no deletion. Legal policy, independent terminal journal
        # acknowledgements and all restorable horizons have not been approved.
        # Expiration alone (including 14 days) is never sufficient authority.
        raise BackupBlocked("RETENTION_APPROVAL_AND_DEPENDENCIES_REQUIRED")


def publish_verified(
    store: BackupStorage,
    backup_id: UUID,
    manifest: Manifest,
    ciphertext: bytes,
    identity: str,
) -> None:
    store.publish(backup_id, ciphertext)
    remote = store.read(backup_id)
    if digest(remote) != digest(ciphertext) or unseal(remote, identity)[0] != manifest:
        raise BackupBlocked("REMOTE_VERIFICATION")
    store.accept(backup_id, manifest, digest(ciphertext))


def diagnostics_receipt(
    path: Path,
    *,
    durable_offsite_verified: bool,
    snapshot_at: datetime | None = None,
    verified: bool = False,
) -> None:
    # MVP-040 v2 distinguishes actual snapshot age, operation and trust scope.
    if durable_offsite_verified and (not verified or snapshot_at is None):
        raise BackupBlocked("DURABLE_VERIFICATION_REQUIRED")
    atomic_write(
        path.parent,
        path.name,
        json.dumps(
            {
                "format": 2,
                "status": "success" if durable_offsite_verified else "failure",
                "checked_at": datetime.now(UTC).isoformat(),
                "snapshot_at": snapshot_at.isoformat() if snapshot_at else None,
                "scope": "offsite" if durable_offsite_verified else "synthetic",
                "operation_status": "verified" if verified else "failure",
            }
        ).encode(),
    )


def check_age(snapshot_at: datetime, now: datetime) -> None:
    if snapshot_at > now or now - snapshot_at > timedelta(hours=24):
        raise BackupBlocked("BACKUP_STALE")
