"""Internal quarantine -> validation -> ClamAV -> verified, never publication.

Trusted: application code/configuration and owner-managed storage/signatures.
Untrusted: all input bytes, names, MIME, lengths, keys and returned DTOs. A DTO is
not authority: prepare() rereads private evidence and rescans an immutable copy.
"""

import hashlib
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

from app.modules.kb.draft_service import DraftFile
from app.modules.kb.files import storage
from app.modules.kb.files.formats import MIMES, validate
from app.modules.kb.files.process import FileRejected
from app.modules.kb.files.sandbox import sealed_snapshot
from app.modules.kb.files.scanner import ClamAV, ScanStamp


@dataclass(frozen=True, repr=False)
class VerifiedFile:
    file_key: str
    mime: str
    size_bytes: int
    checksum_sha256: str
    scan: ScanStamp

    def __repr__(self) -> str:
        return "VerifiedFile(technical_only)"


class FilePipeline:
    def __init__(self, root: Path, *, _scanner: ClamAV | None = None) -> None:
        # Dependencies and root are trusted composition, never upload parameters.
        self.root = root
        self.scanner = _scanner if _scanner is not None else ClamAV()

    @classmethod
    def from_environment(cls) -> "FilePipeline":
        return cls(Path(os.environ.get("APP_FILES_DIR", "/var/lib/akbrizcase/files")))

    @contextmanager
    def _areas(self) -> Iterator[tuple[int, int, int]]:
        descriptors: list[int] = []
        try:
            descriptors.append(storage.open_root(self.root))
            descriptors.append(storage.open_area(descriptors[-1], "documents"))
            root = descriptors[-1]
            for name in ("quarantine", "verified", "receipts"):
                descriptors.append(storage.open_area(root, name))
            yield descriptors[-3], descriptors[-2], descriptors[-1]
        except OSError:
            raise FileRejected("STORAGE_FAILURE") from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def accept(
        self,
        stream: BinaryIO,
        *,
        filename: str,
        mime: str,
        declared_size: int | None = None,
    ) -> VerifiedFile:
        if declared_size is not None and not 1 <= declared_size <= storage.MAX_BYTES:
            raise FileRejected("FILE_SIZE")
        key = secrets.token_hex(32)
        with self._areas() as (quarantine, verified, receipts):
            # No exists()/open race, no retries that overwrite another writer.
            descriptor = os.open(
                key,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=quarantine,
            )
            size = 0
            with os.fdopen(descriptor, "wb") as file:
                while True:
                    try:
                        chunk = stream.read(min(65536, storage.MAX_BYTES + 1 - size))
                    except Exception:
                        raise FileRejected("STREAM_FAILURE") from None
                    if not isinstance(chunk, bytes):
                        raise FileRejected("STREAM_FAILURE")
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > storage.MAX_BYTES:
                        raise FileRejected("FILE_SIZE")
                    file.write(chunk)
                if size == 0 or (declared_size is not None and size != declared_size):
                    raise FileRejected("FILE_SIZE")
                file.flush()
                os.fsync(file.fileno())
                os.fchmod(file.fileno(), 0o400)
            data = storage.read(quarantine, key)
            if len(data) != size:
                raise FileRejected("FILE_CHANGED")
            checksum = hashlib.sha256(data).hexdigest()
            server_mime = validate(data, filename, mime)
            snapshot = sealed_snapshot(data)
            try:
                stamp = self.scanner.scan(snapshot)
            finally:
                os.close(snapshot)
            result = VerifiedFile(key, server_mime, size, checksum, stamp)
            # New exclusive files, no rename-overwrite. A failed receipt write can
            # leave an orphan, but never usable evidence or altered existing data.
            storage.create(verified, key, data)
            storage.create(receipts, key, json.dumps(asdict(result)).encode("ascii"))
            return result

    @contextmanager
    def prepare(self, file_key: str) -> Iterator[tuple[DraftFile, int]]:
        """MVP-021 boundary: a freshly scanned sealed fd + checked draft metadata.

        Callers cannot assert clean or authorize access by constructing a DTO.
        Keep this context open while parsing/binding; no public read API exists.
        A future file route must independently enforce publication + ACL.
        """
        storage.valid_key(file_key)
        with self._areas() as (_, verified, receipts):
            try:
                evidence = json.loads(storage.read(receipts, file_key, 4096))
                data = storage.read(verified, file_key)
                checksum = hashlib.sha256(data).hexdigest()
                if (
                    evidence["file_key"] != file_key
                    or evidence["size_bytes"] != len(data)
                    or evidence["checksum_sha256"] != checksum
                    or evidence["mime"] not in MIMES.values()
                ):
                    raise FileRejected("FILE_CHANGED")
            except FileRejected:
                raise
            except (ValueError, KeyError, TypeError):
                raise FileRejected("EVIDENCE_INVALID") from None
            extension = next(k for k, v in MIMES.items() if v == evidence["mime"])
            validate(data, "verified." + extension, evidence["mime"])
            descriptor = sealed_snapshot(data)
            try:
                # Repeat real scanning against current valid signatures. Stored
                # timestamps or a caller's scan='clean' are never trusted.
                self.scanner.scan(descriptor)
                yield (
                    DraftFile(file_key, evidence["mime"], len(data), checksum),
                    descriptor,
                )
                if (
                    hashlib.sha256(storage.read(verified, file_key)).hexdigest()
                    != checksum
                ):
                    raise FileRejected("FILE_CHANGED")
            finally:
                os.close(descriptor)
