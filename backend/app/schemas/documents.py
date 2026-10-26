"""Published document list; storage/scan/audit evidence never crosses this DTO."""

from typing import Literal

from app.schemas.common import (
    DTO,
    Code,
    Page,
    Pagination,
    PositiveInt,
    Text200,
    UUIDValue,
)

DocumentKind = Literal["manual", "regulation", "article", "other"]


class DocumentQuery(Pagination):
    kind: DocumentKind | None = None


class PublishedDocument(DTO):
    id: UUIDValue
    code: Code
    title: Text200
    kind: DocumentKind
    visibility: Literal["public", "internal"]
    document_version_id: UUIDValue
    version: PositiveInt
    url: str


class DocumentPage(Page[PublishedDocument]):
    pass
