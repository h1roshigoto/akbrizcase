"""Operator-supplied attestations, never generated expert approval.

References are opaque IDs in the protected approval register, not arbitrary URLs,
paths or prose. The CLI never echoes evidence or writes document text to audit.
"""

from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.modules.kb.import_manifest import StrictModel

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Reference = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,199}$")]
Identifier = Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]


class ContentBinding(StrictModel):
    version_id: Identifier
    checksum_sha256: Digest
    file_binding_sha256: Digest
    sections_sha256: Digest

    @model_validator(mode="after")
    def identifier(self) -> "ContentBinding":
        UUID(self.version_id)
        return self


class ReviewEvidence(ContentBinding):
    expert_name: Annotated[str, Field(min_length=3, max_length=200)]
    expert_reference: Reference
    expert_approved_on: str
    decision: Literal["approved"]
    content_checked: bool
    full_file_public_allowed: bool = False
    public_release_reference: Reference | None = None

    @model_validator(mode="after")
    def approval(self) -> "ReviewEvidence":
        if not self.content_checked:
            raise ValueError("CONTENT_REVIEW_REQUIRED")
        name = self.expert_name
        if name != name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError("EXPERT_INVALID")
        approved = date.fromisoformat(self.expert_approved_on)
        if (
            approved.isoformat() != self.expert_approved_on
            or approved > datetime.now(UTC).date()
        ):
            raise ValueError("APPROVAL_DATE_INVALID")
        if self.full_file_public_allowed != (self.public_release_reference is not None):
            raise ValueError("PUBLIC_APPROVAL_REQUIRED")
        return self


class PublicReleaseEvidence(StrictModel):
    document_id: Identifier
    full_files_public_allowed: bool
    release_reference: Reference
    # Visibility opens every approved retired exact link too. Bind the whole set.
    versions: Annotated[list[ContentBinding], Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def identities(self) -> "PublicReleaseEvidence":
        if not self.full_files_public_allowed:
            raise ValueError("PUBLIC_APPROVAL_REQUIRED")
        UUID(self.document_id)
        ids = [v.version_id for v in self.versions]
        if len(ids) != len(set(ids)):
            raise ValueError("VERSION_DUPLICATE")
        return self
