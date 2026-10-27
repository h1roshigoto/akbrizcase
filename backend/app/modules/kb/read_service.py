"""ACL-before-rank/count/page, exact saved excerpts and revalidated sources."""

from uuid import UUID

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from app.modules.kb.access import Viewer, accessible_version
from app.modules.kb.models import Document, DocumentSection, DocumentVersion
from app.schemas.documents import DocumentPage, DocumentQuery, PublishedDocument
from app.schemas.sources import KnowledgeHit, KnowledgePage, KnowledgeQuery, SourceRef

EXCERPT_CHARACTERS = 600
PREVIOUS_EDITION = " (предыдущая редакция)"


class SourceNotFound(Exception):
    pass


def file_url(document_id: UUID, version_id: UUID) -> str:
    return f"/api/v1/documents/{document_id}/versions/{version_id}/file"


def source_ref(
    document: Document, version: DocumentVersion, section: DocumentSection
) -> SourceRef:
    title = document.title
    if version.status == "retired":
        title = title[: 200 - len(PREVIOUS_EDITION)] + PREVIOUS_EDITION
    return SourceRef(
        section_id=section.id,
        document_id=document.id,
        document_version_id=version.id,
        title=title,
        section=section.title,
        page_from=section.page_from,
        page_to=section.page_to,
        url=file_url(document.id, version.id),
    )


class KnowledgeReadService:
    def __init__(self, session: Session, viewer: Viewer) -> None:
        self.session = session
        self.viewer = viewer

    def search(self, query: KnowledgeQuery) -> KnowledgePage:
        if not query.q.strip():
            return KnowledgePage(
                items=[], total=0, limit=query.limit, offset=query.offset
            )
        tsquery = func.websearch_to_tsquery("russian", query.q)
        # MATERIALIZED is a ranking fence: no ts_rank_cd receives a hidden row.
        # The stored vector's @@ predicate remains inside for GIN eligibility.
        allowed = (
            select(
                DocumentSection.id.label("section_id"),
                Document.id.label("document_id"),
                DocumentVersion.id.label("document_version_id"),
                Document.title.label("title"),
                DocumentSection.title.label("section"),
                DocumentSection.page_from,
                DocumentSection.page_to,
                func.substr(DocumentSection.text, 1, EXCERPT_CHARACTERS).label(
                    "excerpt"
                ),
                DocumentSection.search_vector,
            )
            .select_from(DocumentSection)
            .join(
                DocumentVersion,
                DocumentVersion.id == DocumentSection.document_version_id,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                accessible_version(self.viewer),
                # Knowledge is assistant content for both viewers. Staff file
                # and document reads may still access the full approved file.
                DocumentSection.assistant_allowed.is_(True),
                DocumentSection.search_vector.op("@@")(tsquery),
            )
            .cte("allowed_matches")
            .prefix_with("MATERIALIZED")
        )
        rank = func.ts_rank_cd(allowed.c.search_vector, tsquery).label("rank")
        page = (
            select(*[c for c in allowed.c if c.key != "search_vector"], rank)
            .order_by(rank.desc(), allowed.c.section_id)
            .limit(query.limit)
            .offset(query.offset)
            .cte("page")
        )
        total = select(func.count().label("total")).select_from(allowed).cte("total")
        # One PostgreSQL statement/snapshot even for an empty or past-end page.
        rows = (
            self.session.execute(
                select(total.c.total, page)
                .select_from(total.outerjoin(page, true()))
                .order_by(page.c.rank.desc(), page.c.section_id)
            )
            .mappings()
            .all()
        )
        items = [
            KnowledgeHit(
                excerpt=row["excerpt"],
                source=SourceRef(
                    **{key: row[key] for key in SourceRef.model_fields if key != "url"},
                    url=file_url(row["document_id"], row["document_version_id"]),
                ),
            )
            for row in rows
            if row["section_id"] is not None
        ]
        return KnowledgePage(
            items=items, total=rows[0]["total"], limit=query.limit, offset=query.offset
        )

    def documents(self, query: DocumentQuery) -> DocumentPage:
        allowed = (
            select(
                Document.id,
                Document.code,
                Document.title,
                Document.kind,
                Document.visibility,
                DocumentVersion.id.label("document_version_id"),
                DocumentVersion.version,
            )
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(accessible_version(self.viewer))
        )
        if query.kind is not None:
            allowed = allowed.where(Document.kind == query.kind)
        permitted = allowed.cte("allowed_documents").prefix_with("MATERIALIZED")
        page = (
            select(permitted)
            .order_by(permitted.c.title, permitted.c.id)
            .limit(query.limit)
            .offset(query.offset)
            .cte("page")
        )
        total = select(func.count().label("total")).select_from(permitted).cte("total")
        rows = (
            self.session.execute(
                select(total.c.total, page)
                .select_from(total.outerjoin(page, true()))
                .order_by(page.c.title, page.c.id)
            )
            .mappings()
            .all()
        )
        return DocumentPage(
            items=[
                PublishedDocument(
                    **{
                        key: row[key]
                        for key in PublishedDocument.model_fields
                        if key != "url"
                    },
                    url=file_url(row["id"], row["document_version_id"]),
                )
                for row in rows
                if row["id"] is not None
            ],
            total=rows[0]["total"],
            limit=query.limit,
            offset=query.offset,
        )

    def get_source(self, section_id: UUID) -> SourceRef:
        row = self.session.execute(
            select(Document, DocumentVersion, DocumentSection)
            .select_from(DocumentSection)
            .join(
                DocumentVersion,
                DocumentVersion.id == DocumentSection.document_version_id,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                DocumentSection.id == section_id,
                DocumentSection.assistant_allowed.is_(True),
                accessible_version(self.viewer, exact=True),
            )
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            raise SourceNotFound
        return source_ref(*row)

    def exact_version(self, document_id: UUID, version_id: UUID) -> DocumentVersion:
        row = self.session.scalar(
            select(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                Document.id == document_id,
                DocumentVersion.id == version_id,
                accessible_version(self.viewer, exact=True),
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise SourceNotFound
        return row
