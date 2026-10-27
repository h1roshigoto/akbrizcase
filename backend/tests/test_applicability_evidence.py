"""Fail-closed attestations and no bypasses at the operator boundary."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.catalog.applicability_evidence import (
    ApplicabilityPublication,
    ApplicabilityReview,
)
from app.modules.kb.cli import main


def evidence() -> dict[str, object]:
    return {
        "item_id": str(uuid4()),
        "model_id": str(uuid4()),
        "evidence_section_id": str(uuid4()),
        "expert_name": "SYNTHETIC TEST Expert",
        "review_reference": "SYNTHETIC:REVIEW:024",
        "approved_at": "2025-01-01T00:00:00+00:00",
        "decision": "approved",
        "applicability_checked": True,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("expert_name", "  "),
        ("expert_name", "Synthetic\nExpert"),
        ("review_reference", "https://example.invalid/?secret=value"),
        ("review_reference", "/private/path"),
        ("approved_at", "2025-01-01"),
        ("approved_at", "2999-01-01T00:00:00Z"),
        ("approved_at", "infinity"),
        ("applicability_checked", False),
        ("applicability_checked", "true"),
        ("decision", "unchecked"),
        ("actor_id", str(uuid4())),
        ("public_allowed", True),
        ("evidence_section_id", "a" * 36),
    ],
)
def test_review_rejects_missing_or_fabricated_permission(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        ApplicabilityReview.model_validate({**evidence(), field: value})


def test_publication_requires_separate_bound_decision() -> None:
    review = evidence()
    ApplicabilityReview.model_validate(review)
    with pytest.raises(ValidationError):
        ApplicabilityPublication.model_validate(review)
    publication = {
        **review,
        "review_event_id": str(uuid4()),
        "fact_publication_allowed": True,
        "publication_reference": "SYNTHETIC:PUBLIC:024",
    }
    ApplicabilityPublication.model_validate(publication)
    with pytest.raises(ValidationError):
        ApplicabilityPublication.model_validate(
            {**publication, "fact_publication_allowed": False}
        )


@pytest.mark.parametrize(
    "bypass",
    ["--actor-id", "--skip-review", "--public-allowed", "--password", "--force"],
)
def test_applicability_cli_rejects_bypasses_without_echo(
    bypass: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "applicability-publish",
                "--item",
                str(uuid4()),
                "--model",
                str(uuid4()),
                "--evidence",
                "private.json",
                bypass,
                "secret-never-echo",
            ]
        )
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "secret-never-echo" not in output.err + output.out
