"""Server-generated exact-version links; ACL remains a service responsibility."""

from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import (
    DTO,
    KnowledgePagination,
    Page,
    PositiveInt,
    Query,
    Text200,
    UUIDValue,
)


class SourceRef(DTO):
    section_id: UUIDValue
    document_id: UUIDValue
    document_version_id: UUIDValue
    title: Text200
    section: Text200
    page_from: PositiveInt | None
    page_to: PositiveInt | None
    url: str = Field(
        max_length=150,
        pattern=r"^/api/v1/documents/[0-9a-f-]{36}/versions/[0-9a-f-]{36}/file$",
    )

    @model_validator(mode="after")
    def exact_version_and_pages(self) -> "SourceRef":
        expected = (
            f"/api/v1/documents/{self.document_id}/versions/"
            f"{self.document_version_id}/file"
        )
        if self.url != expected:
            raise ValueError("source URL must identify its document and exact version")
        if (self.page_from is None) != (self.page_to is None):
            raise ValueError("pages must both be null or both present")
        if self.page_from is not None and self.page_to is not None:
            if self.page_from > self.page_to:
                raise ValueError("page_from must not exceed page_to")
        return self


class Applicability(DTO):
    model_id: UUIDValue
    status: Literal["confirmed", "unknown"]
    sources: list[SourceRef]

    @model_validator(mode="after")
    def unknown_has_no_sources(self) -> "Applicability":
        if self.status == "unknown" and self.sources:
            raise ValueError("unknown applicability cannot disclose sources")
        return self


class KnowledgeQuery(KnowledgePagination):
    q: Query


class KnowledgeHit(DTO):
    excerpt: str = Field(min_length=1, max_length=10000)
    source: SourceRef


class KnowledgePage(Page[KnowledgeHit]):
    limit: int = Field(ge=1, le=5)
