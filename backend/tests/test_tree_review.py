"""Strict expert attestations and non-echoing lifecycle CLI argument rejection."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.kb.cli import main
from app.modules.kb.tree_lifecycle import TreeReviewEvidence


def evidence() -> dict[str, object]:
    return {
        "tree_id": str(uuid4()),
        "yaml_checksum": "a" * 64,
        "binding_sha256": "b" * 64,
        "expert_name": "SYNTHETIC ONLY Expert",
        "review_reference": "SYNTHETIC:028:REVIEW",
        "approved_at": datetime.now(UTC).isoformat(),
        "decision": "approved",
        "content_checked": True,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_checked", False),
        ("content_checked", "true"),
        ("content_checked", 1),
        ("decision", "rejected"),
        ("expert_name", "\nSynthetic"),
        ("expert_name", "Synthetic\u200b"),
        ("review_reference", "/private/path"),
        ("approved_at", "2999-01-01T00:00:00Z"),
        ("approved_at", "2026-01-01"),
        ("approved_at", "infinity"),
        ("tree_id", "a" * 36),
        ("yaml_checksum", "clean"),
        ("published_by", str(uuid4())),
        ("binding_sha256", "0"),
    ],
)
def test_strict_expert_evidence(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        TreeReviewEvidence.model_validate({**evidence(), field: value})


@pytest.mark.parametrize(
    "bypass",
    [
        "--actor-id",
        "--force",
        "--skip-review",
        "--skip-validation",
        "--password",
    ],
)
def test_tree_lifecycle_cli_rejects_bypasses_without_echo(
    bypass: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "tree-publish",
                "--tree",
                str(uuid4()),
                "--file",
                "/synthetic.yaml",
                bypass,
                "SECRET-SYNTHETIC",
            ]
        )
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "SECRET-SYNTHETIC" not in output.out + output.err
