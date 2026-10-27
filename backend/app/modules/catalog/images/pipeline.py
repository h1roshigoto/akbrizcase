"""Bounded quarantine, double AV, isolated re-encoding and immutable storage."""

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

from app.modules.kb.files import storage
from app.modules.kb.files.process import FileRejected, Limits
from app.modules.kb.files.sandbox import run_parser, sealed_snapshot
from app.modules.kb.files.scanner import ClamAV, ScanStamp

MAX_BYTES = 5 * 1024**2
IMAGE_LIMITS = Limits(cpu=3, memory=384 * 1024**2, seconds=5, output=MAX_BYTES)
KEY = re.compile(r"[a-f0-9]{64}\.(jpg|png)\Z")
MIMES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}


def valid_key(key: str) -> None:
    if not KEY.fullmatch(key):
        raise FileRejected("IMAGE_KEY")


def signature(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    raise FileRejected("IMAGE_FORMAT")


def create(directory: int, key: str, data: bytes) -> None:
    valid_key(key)
    descriptor = os.open(
        key,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory,
    )
    with os.fdopen(descriptor, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
        os.fchmod(file.fileno(), 0o400)
    os.fsync(directory)


def read(directory: int, key: str, maximum: int = MAX_BYTES) -> bytes:
    valid_key(key)
    descriptor = os.open(
        key,
        os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=directory,
    )
    with os.fdopen(descriptor, "rb") as file:
        metadata = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or not 0 < metadata.st_size <= maximum
        ):
            raise FileRejected("STORAGE_FILE")
        data = file.read(maximum + 1)
        if len(data) != metadata.st_size:
            raise FileRejected("FILE_CHANGED")
        return data


@dataclass(frozen=True, repr=False)
class VerifiedImage:
    image_key: str
    mime: str
    size_bytes: int
    checksum_sha256: str
    original_scan: ScanStamp
    output_scan: ScanStamp


class ImagePipeline:
    def __init__(self, root: Path, *, _scanner: ClamAV | None = None) -> None:
        self.root = root
        self.scanner = _scanner if _scanner is not None else ClamAV()

    @classmethod
    def from_environment(cls) -> "ImagePipeline":
        return cls(Path(os.environ.get("APP_FILES_DIR", "/var/lib/akbrizcase/files")))

    @contextmanager
    def _areas(self) -> Iterator[tuple[int, int, int]]:
        descriptors: list[int] = []
        try:
            descriptors.append(storage.open_root(self.root))
            descriptors.append(storage.open_area(descriptors[-1], "images"))
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
    ) -> VerifiedImage:
        extension = filename.rsplit(".", 1)[-1].lower()
        if (
            "/" in filename
            or "\\" in filename
            or "\x00" in filename
            or extension not in MIMES
            or MIMES[extension] != mime
        ):
            raise FileRejected("IMAGE_FORMAT")
        if declared_size is not None and not 0 < declared_size <= MAX_BYTES:
            raise FileRejected("FILE_SIZE")
        key = secrets.token_hex(32) + (".jpg" if mime == "image/jpeg" else ".png")
        with self._areas() as (quarantine, verified, receipts):
            # A bounded stream is written to a new private quarantine object.
            fd = os.open(
                key,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=quarantine,
            )
            size = 0
            with os.fdopen(fd, "wb") as file:
                while True:
                    try:
                        chunk = stream.read(min(65536, MAX_BYTES + 1 - size))
                    except Exception:
                        raise FileRejected("STREAM_FAILURE") from None
                    if not isinstance(chunk, bytes):
                        raise FileRejected("STREAM_FAILURE")
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise FileRejected("FILE_SIZE")
                    file.write(chunk)
                if not size or (declared_size is not None and size != declared_size):
                    raise FileRejected("FILE_SIZE")
                file.flush()
                os.fsync(file.fileno())
                os.fchmod(file.fileno(), 0o400)
            data = read(quarantine, key)
            if signature(data) != mime:
                raise FileRejected("IMAGE_FORMAT")
            original = sealed_snapshot(data)
            try:
                original_scan = self.scanner.scan(original)
                result = run_parser(original, "image", limits=IMAGE_LIMITS)
            finally:
                os.close(original)
            if not 0 < len(result) <= MAX_BYTES or signature(result) != mime:
                raise FileRejected("IMAGE_OUTPUT")
            output = sealed_snapshot(result)
            try:
                output_scan = self.scanner.scan(output)
                run_parser(output, "image-check", limits=IMAGE_LIMITS)
            finally:
                os.close(output)
            accepted = VerifiedImage(
                key,
                mime,
                len(result),
                hashlib.sha256(result).hexdigest(),
                original_scan,
                output_scan,
            )
            create(verified, key, result)
            create(receipts, key, json.dumps(asdict(accepted)).encode("ascii"))
            # Reopen using the same strict file metadata/checksum boundary.
            self._checked(verified, receipts, key)
            return accepted

    @staticmethod
    def _checked(verified: int, receipts: int, key: str) -> tuple[VerifiedImage, bytes]:
        try:
            evidence = json.loads(read(receipts, key, 4096))
            data = read(verified, key)
            mime = "image/jpeg" if key.endswith(".jpg") else "image/png"
            if (
                evidence["image_key"] != key
                or evidence["mime"] != mime
                or evidence["size_bytes"] != len(data)
                or evidence["checksum_sha256"] != hashlib.sha256(data).hexdigest()
                or signature(data) != mime
            ):
                raise FileRejected("FILE_CHANGED")
            return VerifiedImage(
                key,
                mime,
                len(data),
                evidence["checksum_sha256"],
                ScanStamp(**evidence["original_scan"]),
                ScanStamp(**evidence["output_scan"]),
            ), data
        except FileRejected:
            raise
        except (ValueError, KeyError, TypeError):
            raise FileRejected("EVIDENCE_INVALID") from None

    def metadata(self, key: str) -> VerifiedImage:
        valid_key(key)
        with self._areas() as (_, verified, receipts):
            return self._checked(verified, receipts, key)[0]

    @contextmanager
    def prepare(self, key: str) -> Iterator[tuple[VerifiedImage, int]]:
        """Fresh scan/full decode, sealed snapshot and exit integrity check.

        Complete expensive work before acquiring any catalog/staff row locks.
        Exit this context inside the binding UoW, before its commit.
        """
        valid_key(key)
        with self._areas() as (_, verified, receipts):
            metadata, data = self._checked(verified, receipts, key)
            descriptor = sealed_snapshot(data)
            try:
                self.scanner.scan(descriptor)
                run_parser(descriptor, "image-check", limits=IMAGE_LIMITS)
                yield metadata, descriptor
                if self._checked(verified, receipts, key)[0] != metadata:
                    raise FileRejected("FILE_CHANGED")
            finally:
                os.close(descriptor)
