"""Canonical fingerprints, bounded attestations and safe operator parsing."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.kb.cli import main
from app.modules.kb.files import FileRejected
from app.modules.kb.import_manifest import checked_record
from app.modules.kb.lifecycle_evidence import ReviewEvidence
from app.modules.kb.lifecycle_service import section_fingerprint
from app.modules.kb.models import DocumentSection


def evidence() -> dict[str, object]:
    return {
        "version_id": str(uuid4()),
        "checksum_sha256": "a" * 64,
        "file_binding_sha256": "b" * 64,
        "sections_sha256": "c" * 64,
        "expert_name": "Synthetic Expert",
        "expert_reference": "SYNTHETIC:REVIEW:1",
        "expert_approved_on": "2026-01-01",
        "decision": "approved",
        "content_checked": True,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_checked", False),
        ("decision", "unchecked"),
        ("expert_name", "\nSynthetic"),
        ("expert_reference", "/private/path"),
        ("expert_approved_on", "2999-01-01"),
        ("expert_approved_on", "20260101"),
        ("full_file_public_allowed", True),
        ("public_release_reference", "SYNTHETIC:1"),
        ("reviewed_by", str(uuid4())),
        ("approved_at", "2026-01-01"),
        ("version_id", "a" * 36),
        ("checksum_sha256", "clean"),
        ("content_checked", "true"),
        ("content_checked", 1),
    ],
)
def test_review_requires_operator_evidence(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ReviewEvidence.model_validate({**evidence(), field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("section_key", "changed"),
        ("title", "changed"),
        ("page_from", 2),
        ("page_to", 3),
        ("text", "changed\n"),
        ("assistant_allowed", False),
    ],
)
def test_fingerprint_binds_every_field(field: str, value: object) -> None:
    first = DocumentSection(
        section_key="a",
        title="Синтетический",
        text="x\n",
        page_from=1,
        page_to=1,
        assistant_allowed=True,
    )
    other = DocumentSection(
        section_key="b", title="Second", text="y", assistant_allowed=False
    )
    original = section_fingerprint([first, other])
    assert original == section_fingerprint([other, first])
    setattr(first, field, value)
    assert original != section_fingerprint([first, other])
    assert original != section_fingerprint([other])


def test_private_evidence_exit_detects_replacement(tmp_path: Path) -> None:
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(evidence()))
    path.chmod(0o600)
    with pytest.raises(FileRejected, match="MANIFEST_CHANGED"):
        with checked_record(path, ReviewEvidence):
            replacement = tmp_path / "replacement"
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(path)


@pytest.mark.parametrize(
    "bypass",
    [
        "--actor-id",
        "--force",
        "--skip-review",
        "--skip-scan",
        "--published=true",
        "--password",
    ],
)
def test_lifecycle_cli_rejects_bypasses_without_echo(
    bypass: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "kb-publish",
                "--document",
                str(uuid4()),
                "--version",
                str(uuid4()),
                bypass,
                "SENSITIVE",
            ]
        )
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "SENSITIVE" not in output.err + output.out
    assert bypass not in output.err + output.out
