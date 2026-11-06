"""Bounded age v1 archives; private metadata is inside authenticated ciphertext.

pyrage delegates to the maintained Rust age implementation. No bespoke cipher,
nonce management, password KDF or unauthenticated outer metadata is used.
"""

import hashlib
import io
import json
import os
import re
import stat
import tarfile
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

import pyrage  # type: ignore[import-untyped]
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

MAX_ARCHIVE = 256 * 1024**2
SHA = r"^[a-f0-9]{64}$"
PATH = re.compile(
    r"^(?:documents/(?:verified|receipts)/[a-f0-9]{64}"
    r"|images/(?:verified|receipts)/[a-f0-9]{64}\.(?:jpg|png))$"
)


class BackupBlocked(ValueError):
    """Only fixed codes cross the operator output boundary."""


class FileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(pattern=PATH.pattern)
    sha256: str = Field(pattern=SHA)
    size: int = Field(strict=True, gt=0, le=25 * 1024**2)
    mode: Literal[256] = 0o400


class SequenceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    epoch: UUID
    fingerprint: str = Field(pattern=SHA)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal[1] = 1
    schema_revision: str
    application_head: str = Field(pattern=r"^[a-f0-9]{40}$")
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    snapshot_at: AwareDatetime
    postgres_major: Literal[16] = 16
    dump_sha256: str = Field(pattern=SHA)
    dump_size: int = Field(strict=True, gt=0, le=MAX_ARCHIVE)
    sequence_floor: int = Field(default=0, strict=True, ge=0)
    sequence_source: SequenceIdentity | None = None
    files: tuple[FileEntry, ...] = Field(max_length=10000)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: BaseModel) -> bytes:
    payload = value.model_dump(mode="json")
    # Existing v1 archives remain readable, but have no sequence admission proof.
    if isinstance(value, Manifest) and value.sequence_source is None:
        payload.pop("sequence_source")
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def private_read(path: Path, maximum: int, *, mode: int = 0o600) -> bytes:
    # Anchor every component, including the parent, against symlink substitution.
    from app.modules.kb.files.storage import DIRECTORY

    if not path.is_absolute() or ".." in path.parts:
        raise BackupBlocked("PRIVATE_FILE_REQUIRED")
    parent = os.open("/", DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, DIRECTORY, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != mode
                or not 0 < before.st_size <= maximum
            ):
                raise BackupBlocked("PRIVATE_FILE_REQUIRED")
            data = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
            if (
                len(data) != before.st_size
                or len(data) > maximum
                or (
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise BackupBlocked("FILE_CHANGED")
            return data
    finally:
        os.close(parent)


def seal(
    manifest: Manifest, dump: bytes, files: dict[str, bytes], recipient: str
) -> bytes:
    validate_members(manifest, dump, files)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in {
            "manifest.json": canonical(manifest),
            "database.dump": dump,
            **files,
        }.items():
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), 0o400
            tar.addfile(member, io.BytesIO(data))
    if archive.tell() > MAX_ARCHIVE:
        raise BackupBlocked("ARCHIVE_LIMIT")
    return cast(
        bytes,
        pyrage.encrypt(
            archive.getvalue(), [pyrage.x25519.Recipient.from_str(recipient)]
        ),
    )


def validate_members(manifest: Manifest, dump: bytes, files: dict[str, bytes]) -> None:
    if len(dump) != manifest.dump_size or digest(dump) != manifest.dump_sha256:
        raise BackupBlocked("DUMP_INTEGRITY")
    if (
        len({f.path for f in manifest.files}) != len(manifest.files)
        or set(files) != {f.path for f in manifest.files}
        or len(dump) + sum(map(len, files.values())) > MAX_ARCHIVE
    ):
        raise BackupBlocked("MEMBER_SET")
    for entry in manifest.files:
        if (
            len(files[entry.path]) != entry.size
            or digest(files[entry.path]) != entry.sha256
        ):
            raise BackupBlocked("FILE_INTEGRITY")


def unseal(
    ciphertext: bytes, identity: str
) -> tuple[Manifest, bytes, dict[str, bytes]]:
    if not 0 < len(ciphertext) <= MAX_ARCHIVE + 1024**2:
        raise BackupBlocked("ARCHIVE_LIMIT")
    plaintext = pyrage.decrypt(ciphertext, [pyrage.x25519.Identity.from_str(identity)])
    if len(plaintext) > MAX_ARCHIVE:
        raise BackupBlocked("ARCHIVE_LIMIT")
    # Never tar.extract(): links, devices, duplicate names and unknown members fail.
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:") as tar:
        for entry in tar:
            if (
                not entry.isfile()
                or entry.name in members
                or entry.size <= 0
                or entry.mode != 0o400
                or len(members) > 10001
                or entry.size > MAX_ARCHIVE
                or (
                    entry.name not in {"manifest.json", "database.dump"}
                    and not PATH.fullmatch(entry.name)
                )
            ):
                raise BackupBlocked("ARCHIVE_MEMBER")
            stream = tar.extractfile(entry)
            if stream is None:
                raise BackupBlocked("ARCHIVE_MEMBER")
            members[entry.name] = stream.read(entry.size + 1)
    manifest_bytes = members.pop("manifest.json")
    manifest = Manifest.model_validate_json(manifest_bytes)
    if canonical(manifest) != manifest_bytes:
        raise BackupBlocked("MANIFEST_CANONICAL")
    dump = members.pop("database.dump")
    validate_members(manifest, dump, members)
    return manifest, dump, members
