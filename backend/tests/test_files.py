"""Byte validation and private storage guards (no scanner success doubles)."""

import io
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.main import app
from app.modules.kb.files import FilePipeline, FileRejected
from app.modules.kb.files.formats import validate
from app.modules.kb.files.scanner import MAX_SIGNATURE_AGE, ClamAV
from app.modules.kb.files.storage import MAX_BYTES, create, open_root, read, valid_key


@pytest.mark.parametrize(
    ("data", "filename", "mime"),
    [
        (b"text", "a.html", "text/html"),
        (b"text", "a.txt", "application/pdf"),
        (b"wrong", "a.pdf", "application/pdf"),
        (b"%PDF-1.4\n", "a.txt", "text/plain"),
        (b"%PDF-1.4\ncorrupt", "a.pdf", "application/pdf"),
        (b"\xff", "a.txt", "text/plain"),
        (b"\0binary", "a.txt", "text/plain"),
        (b"<!DOCTYPE html><html>private</html>", "a.txt", "text/plain"),
        (b"# Title\n<script>alert(1)</script>", "a.md", "text/markdown"),
        (b"# Title\n<img/src=x onerror=alert(1)>", "a.md", "text/markdown"),
        (b"<script/src=blocked>", "a.txt", "text/plain"),
        (b"[link](javascript:alert(1))", "a.md", "text/markdown"),
        (b"#!/bin/sh\necho private", "a.txt", "text/plain"),
        (b"import os\nos.system('private')", "a.txt", "text/plain"),
        (b"MZexecutable", "a.txt", "text/plain"),
        (b"\x7fELF", "a.txt", "text/plain"),
        (b"PK\x03\x04zip", "a.pdf", "application/pdf"),
        (b"PK\x03\x04zip", "a.txt", "text/plain"),
        (b"\x1f\x8bgzip", "a.txt", "text/plain"),
        (b"x" * 257 + b"ustar", "a.txt", "text/plain"),
        (b"text", "../a.txt", "text/plain"),
        (b"text", "/a.txt", "text/plain"),
        (b"text", "https://example.invalid/a.txt", "text/plain"),
        (b"<svg/>", "a.svg", "image/svg+xml"),
    ],
)
def test_reject_formats(data: bytes, filename: str, mime: str) -> None:
    with pytest.raises(FileRejected) as error:
        validate(data, filename, mime)
    assert str(error.value) == error.value.code
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "key", ["../x", "/etc/passwd", "https://x", "a.txt", "", "g" * 64]
)
def test_keys(key: str) -> None:
    with pytest.raises(FileRejected, match="FILE_KEY"):
        valid_key(key)


def test_exclusive_creation_symlink_and_concurrency(tmp_path: Path) -> None:
    root = open_root(tmp_path)
    key = "a" * 64
    try:

        def write() -> bool:
            try:
                create(root, key, b"synthetic")
                return True
            except FileExistsError:
                return False

        with ThreadPoolExecutor(2) as executor:
            assert sorted(executor.map(lambda _: write(), range(2))) == [False, True]
        assert read(root, key) == b"synthetic"
        with pytest.raises(FileExistsError):
            create(root, key, b"replacement")
        os.symlink("/etc/passwd", tmp_path / ("b" * 64))
        with pytest.raises(OSError):
            read(root, "b" * 64)
        os.link(tmp_path / key, tmp_path / ("c" * 64))
        with pytest.raises(FileRejected):
            read(root, key)
    finally:
        os.close(root)


def test_symlink_root_and_directory_swap(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        open_root(link)
    pipeline = FilePipeline(tmp_path)
    (tmp_path / "documents").symlink_to(target, target_is_directory=True)
    with pytest.raises(FileRejected, match="STORAGE_FAILURE"):
        pipeline.accept(io.BytesIO(b"text"), filename="a.txt", mime="text/plain")


@pytest.mark.parametrize(
    "data,size",
    [(b"", None), (b"xx", 1), (b"x" * (MAX_BYTES + 1), None)],
    ids=["empty", "declared-size", "oversize"],
)
def test_actual_size(tmp_path: Path, data: bytes, size: int | None) -> None:
    pipeline = FilePipeline(tmp_path)
    with pytest.raises(FileRejected, match="FILE_SIZE"):
        pipeline.accept(
            io.BytesIO(data), filename="a.txt", mime="text/plain", declared_size=size
        )
    assert not list((tmp_path / "documents" / "verified").iterdir())


def test_storage_failure(tmp_path: Path) -> None:
    tmp_path.chmod(0o500)
    with pytest.raises(FileRejected):
        FilePipeline(tmp_path).accept(
            io.BytesIO(b"text"), filename="a.txt", mime="text/plain"
        )
    tmp_path.chmod(0o700)


def test_missing_signatures_never_clean(tmp_path: Path) -> None:
    pipeline = FilePipeline(tmp_path, _scanner=ClamAV(tmp_path / "missing"))
    with pytest.raises(FileRejected):
        pipeline.accept(io.BytesIO(b"synthetic"), filename="a.txt", mime="text/plain")
    assert not list((tmp_path / "documents" / "verified").iterdir())


def test_stale_signatures() -> None:
    import time

    for epoch in (0, int(time.time()) - MAX_SIGNATURE_AGE - 1, int(time.time()) + 301):
        with pytest.raises(FileRejected, match="SIGNATURES_STALE"):
            ClamAV.require_fresh(epoch)


def test_private_storage_and_unavailable_documents(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("APP_FILES_DIR", str(tmp_path))
    marker = "synthetic-private-marker"
    (tmp_path / "quarantine.txt").write_text(marker)
    with TestClient(app) as client:
        for path in (
            "/files/quarantine.txt",
            "/api/v1/documents/a/file",
            "/documents/quarantine/a",
        ):
            response = client.get(path)
            assert response.status_code == 404 and marker not in response.text
        # MVP-022 registers this API, but an unavailable DB still fails closed.
        app.state.database = None
        response = client.get("/api/v1/documents")
        assert response.status_code == 503 and marker not in response.text


def test_key_validation_open_race(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    root = open_root(tmp_path)
    key = "d" * 64
    create(root, key, b"synthetic")
    original_open = os.open

    def swapped(path: str, flags: int, *args: object, dir_fd: int | None = None) -> int:
        if path == key and dir_fd == root:
            (tmp_path / key).unlink()
            (tmp_path / key).symlink_to("/etc/passwd")
        return original_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapped)
    try:
        with pytest.raises(OSError):
            read(root, key)
    finally:
        os.close(root)
