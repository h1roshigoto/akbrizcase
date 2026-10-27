"""Private, explicit attestations supplied by an operator; never synthesized."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.modules.kb.import_manifest import StrictModel
from app.modules.kb.lifecycle_evidence import Identifier, Reference


class PairBinding(StrictModel):
    item_id: Identifier
    model_id: Identifier
    evidence_section_id: Identifier

    @model_validator(mode="after")
    def identifiers(self) -> "PairBinding":
        for value in (self.item_id, self.model_id, self.evidence_section_id):
            if UUID(value).version != 4:
                raise ValueError("IDENTIFIER_INVALID")
        return self


class ApplicabilityReview(PairBinding):
    expert_name: Annotated[str, Field(min_length=3, max_length=200)]
    review_reference: Reference
    approved_at: str
    decision: Literal["approved"]
    applicability_checked: bool

    @model_validator(mode="after")
    def checked(self) -> "ApplicabilityReview":
        if not self.applicability_checked:
            raise ValueError("EXPERT_REVIEW_REQUIRED")
        name = self.expert_name
        if name != name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError("EXPERT_INVALID")
        approved = datetime.fromisoformat(self.approved_at)
        if approved.tzinfo is None or approved > datetime.now(UTC):
            raise ValueError("APPROVAL_DATE_INVALID")
        self.approved_at = approved.astimezone(UTC).isoformat()
        return self


class ApplicabilityPublication(ApplicabilityReview):
    review_event_id: Identifier
    fact_publication_allowed: bool
    publication_reference: Reference

    @model_validator(mode="after")
    def explicit_permission(self) -> "ApplicabilityPublication":
        if not self.fact_publication_allowed or UUID(self.review_event_id).version != 4:
            raise ValueError("PUBLIC_APPROVAL_REQUIRED")
        return self
