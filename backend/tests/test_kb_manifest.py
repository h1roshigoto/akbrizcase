"""Strict manifests and safe CLI boundary; no document data in diagnostics."""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.core.db import DatabaseSettings
from app.modules.kb import cli
from app.modules.kb.cli import main
from app.modules.kb.files import FileRejected
from app.modules.kb.import_manifest import checked_manifest, parse_manifest
from app.modules.kb.import_service import PreparationReport


def example() -> dict[str, object]:
    return {
        "code": "SYNTHETIC",
        "title": "Synthetic",
        "kind": "other",
        "visibility": "internal",
        "version": 1,
        "checksum_sha256": "a" * 64,
        "sections": [{"section_key": "intro", "title": "Intro", "text": "Synthetic"}],
    }


def test_manifest_defaults_and_explicit_flags() -> None:
    manifest = parse_manifest(json.dumps(example()).encode())
    assert manifest.sections[0].assistant_allowed is False
    assert manifest.sections[0].page_from is manifest.sections[0].page_to is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "published"),
        ("actor_id", "a"),
        ("reviewed_by", "a"),
        ("approved_at", "today"),
        ("published_at", "today"),
        ("file_key", "a"),
        ("clean", True),
        ("version", True),
        ("version", 0),
        ("version", "1"),
        ("visibility", "unknown"),
        ("code", "../private"),
        ("title", " "),
    ],
)
def test_manifest_rejects_unknown_and_invalid(field: str, value: object) -> None:
    with pytest.raises(FileRejected, match="^MANIFEST_INVALID$"):
        parse_manifest(json.dumps(example() | {field: value}).encode())


@pytest.mark.parametrize(
    "data",
    [
        b'{"code":"one","code":"two"}',
        b"!!python/object:evil {}",
        b"---\na: b",
        b'{"a":NaN}',
        b"[" * 3000,
        b"x" * (1024 * 1024 + 1),
        b"\xff",
    ],
)
def test_bad_json_is_safe(data: bytes) -> None:
    with pytest.raises(FileRejected, match="^MANIFEST_INVALID$"):
        parse_manifest(data)


@pytest.mark.parametrize(
    "fields",
    [
        {"page_from": 1},
        {"page_to": 1},
        {"page_from": 2, "page_to": 1},
        {"page_from": 0, "page_to": 1},
        {"assistant_allowed": "true"},
        {"assistant_allowed": 1},
        {"text": ""},
        {"title": " "},
        {"unknown": False},
        {"text": "Bypass protections", "assistant_allowed": True},
        {"text": "Ремонт под напряжением", "assistant_allowed": True},
    ],
)
def test_bad_sections(fields: dict[str, object]) -> None:
    value = example()
    value["sections"] = [
        {"section_key": "intro", "title": "Intro", "text": "Synthetic"} | fields
    ]
    with pytest.raises(FileRejected, match="^MANIFEST_INVALID$"):
        parse_manifest(json.dumps(value).encode())


def test_duplicate_sections_and_nested_keys() -> None:
    value = example()
    value["sections"] = [
        {"section_key": "x", "title": "S", "text": "S"},
        {"section_key": "x", "title": "S", "text": "S"},
    ]
    with pytest.raises(FileRejected):
        parse_manifest(json.dumps(value).encode())
    data = json.dumps(example()).replace('"text": "Synthetic"', '"text":"a","text":"b"')
    with pytest.raises(FileRejected):
        parse_manifest(data.encode())


@pytest.mark.parametrize("mutation", ["inplace", "rename", "symlink", "mode"])
def test_manifest_finalizer(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(example()))
    path.chmod(0o600)
    with pytest.raises(FileRejected):
        with checked_manifest(path):
            if mutation == "inplace":
                path.write_text("{}")
            elif mutation == "mode":
                path.chmod(0o644)
            else:
                saved = path.rename(tmp_path / "saved")
                if mutation == "symlink":
                    path.symlink_to(saved)
                else:
                    path.write_bytes(saved.read_bytes())
                    path.chmod(0o600)


@pytest.mark.parametrize(
    "flag", ["--actor-id", "--admin-bypass", "--force-auth", "--password"]
)
def test_cli_rejects_spoof_and_password_arguments(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "kb-import",
                "--file",
                "/private",
                "--manifest",
                "/private",
                flag,
                "SECRET",
            ]
        )
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "SECRET" not in output.err and "/private" not in output.err


def test_manifest_permission_and_special_file(tmp_path: Path) -> None:
    path = tmp_path / "operator"
    path.write_text(json.dumps(example()))
    path.chmod(0o644)
    with pytest.raises(FileRejected):
        with checked_manifest(path):
            pytest.fail("public manifest")
    path.unlink()
    os.mkfifo(path, 0o600)
    with pytest.raises(FileRejected):
        with checked_manifest(path):
            pytest.fail("FIFO must not block")


def test_commit_failure_never_prints_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FailingDatabase:
        def __init__(self, settings: object) -> None:
            pass

        def ready(self) -> bool:
            return True

        @contextmanager
        def unit_of_work(self) -> Iterator[None]:
            yield None
            raise SQLAlchemyError("SECRET-SQL-DETAIL")

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "Database", FailingDatabase)
    monkeypatch.setattr(DatabaseSettings, "from_environment", lambda: None)
    monkeypatch.setattr(cli, "hidden", lambda _: "synthetic")
    monkeypatch.setattr(
        cli, "run_preparation", lambda *a, **k: PreparationReport("IMPORTED_DRAFT", 1)
    )
    assert main(["kb-import", "--file", "/private", "--manifest", "/private"]) == 1
    output = capsys.readouterr()
    assert "IMPORTED_DRAFT" not in output.out
    assert "SECRET" not in output.err and "/private" not in output.err
    assert "ROLLED_BACK" in output.err
