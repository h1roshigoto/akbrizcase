"""Internal admin draft preparation and edits. Publication and ACL arrive later.

The caller supplies an authenticated identity and owns the UoW. No generic field
mapping, lifecycle changes, commits, file reads or content-bearing audit entries.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedStaff, has_role
from app.core.staff_models import AuditEvent
from app.modules.kb.models import Document, DocumentSection, DocumentVersion


class DraftEditRejected(ValueError):
    """Safe rejection with no document text, path or file key."""


@dataclass(frozen=True)
class DraftFile:
    file_key: str
    mime: str
    size_bytes: int
    checksum_sha256: str


@dataclass(frozen=True)
class SectionContent:
    title: str
    text: str
    page_from: int | None
    page_to: int | None
    assistant_allowed: bool


@dataclass(frozen=True)
class DraftDocument:
    code: str
    title: str
    kind: str
    visibility: str


@dataclass(frozen=True)
class KeyedSection:
    section_key: str
    content: SectionContent


class DraftEditService:
    def __init__(self, session: Session, actor: AuthenticatedStaff) -> None:
        if not has_role(actor.user, "admin"):
            raise DraftEditRejected("Active admin required")
        self.session = session
        self.actor_id = actor.user.id

    def _draft(self, version_id: UUID) -> DocumentVersion:
        # Documents → versions is the shared lifecycle lock order. Refresh cached
        # ORM state after acquiring the lock, including after a concurrent publish.
        document_id = self.session.scalar(
            select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
        )
        document = self.session.scalar(
            select(Document)
            .where(Document.id == document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        version = self.session.scalar(
            select(DocumentVersion)
            .where(DocumentVersion.id == version_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if document is None or version is None:
            raise DraftEditRejected("Draft not found")
        if document.status != "active" or version.status != "draft":
            raise DraftEditRejected("Only active document drafts can be edited")
        return version

    def _audit(
        self, identifier: UUID, entity: str, action: str, result: str = "success"
    ) -> None:
        self.session.add(
            AuditEvent(
                actor_id=self.actor_id,
                entity_type=entity,
                entity_id=identifier,
                action=action,
                result=result,
                details={},
            )
        )
        self.session.flush()

    def find_import(
        self, document: DraftDocument, number: int
    ) -> DocumentVersion | None:
        parent = self.session.scalar(
            select(Document)
            .where(Document.code == document.code)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if parent is None:
            return None
        if parent.status != "active" or (
            parent.title,
            parent.kind,
            parent.visibility,
        ) != (document.title, document.kind, document.visibility):
            raise DraftEditRejected("DOCUMENT_CONFLICT")
        version = self.session.scalar(
            select(DocumentVersion)
            .where(
                DocumentVersion.document_id == parent.id,
                DocumentVersion.version == number,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if version is not None:
            self._draft(version.id)
            if any(
                value is not None
                for value in (
                    version.reviewed_by,
                    version.approved_at,
                    version.published_at,
                )
            ):
                raise DraftEditRejected("DRAFT_REVIEW_CONFLICT")
        return version

    def import_draft(
        self, document: DraftDocument, number: int, content: DraftFile
    ) -> DocumentVersion:
        # UNIQUE + conflict-safe insert serializes even a not-yet-existing code.
        # All lifecycle writers retain the document → version → section order.
        self.session.execute(
            insert(Document)
            .values(
                code=document.code,
                title=document.title,
                kind=document.kind,
                visibility=document.visibility,
                status="active",
            )
            .on_conflict_do_nothing(index_elements=[Document.code])
        )
        version = self.find_import(document, number)
        if version is not None:
            if version.checksum_sha256 != content.checksum_sha256:
                raise DraftEditRejected("CHECKSUM_CONFLICT")
            if (version.mime, version.size_bytes) != (content.mime, content.size_bytes):
                raise DraftEditRejected("FILE_METADATA_CONFLICT")
            return version
        parent_id = self.session.scalar(
            select(Document.id).where(Document.code == document.code)
        )
        assert parent_id is not None
        version = DocumentVersion(
            document_id=parent_id,
            version=number,
            file_key=content.file_key,
            mime=content.mime,
            size_bytes=content.size_bytes,
            checksum_sha256=content.checksum_sha256,
            status="draft",
        )
        self.session.add(version)
        self.session.flush()
        return version

    def reconcile_sections(
        self, version_id: UUID, expected: list[KeyedSection], *, check: bool = False
    ) -> bool:
        self._draft(version_id)
        stored = self.session.scalars(
            select(DocumentSection)
            .where(DocumentSection.document_version_id == version_id)
            .order_by(DocumentSection.section_key)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        actual = [
            KeyedSection(
                s.section_key,
                SectionContent(
                    s.title, s.text, s.page_from, s.page_to, s.assistant_allowed
                ),
            )
            for s in stored
        ]
        wanted = sorted(expected, key=lambda s: s.section_key)
        if len({s.section_key for s in wanted}) != len(wanted):
            raise DraftEditRejected("SECTION_DUPLICATE")
        if actual == wanted:
            return False
        if stored or check:
            raise DraftEditRejected("SECTIONS_CONFLICT")
        # Only the precisely defined empty draft recovery is permitted. Existing
        # sections are never silently overwritten or partially replaced.
        for section in wanted:
            c = section.content
            self.session.add(
                DocumentSection(
                    document_version_id=version_id,
                    section_key=section.section_key,
                    title=c.title,
                    text=c.text,
                    page_from=c.page_from,
                    page_to=c.page_to,
                    assistant_allowed=c.assistant_allowed,
                )
            )
        self.session.flush()
        return True

    def record_import(self, version_id: UUID, action: str, result: str) -> None:
        self._audit(version_id, "document_version", action, result)

    def replace_file(self, version_id: UUID, content: DraftFile) -> None:
        with self.session.no_autoflush:
            version = self._draft(version_id)
            version.file_key = content.file_key
            version.mime = content.mime
            version.size_bytes = content.size_bytes
            version.checksum_sha256 = content.checksum_sha256
        self._audit(version_id, "document_version", "document_version.edit_draft_file")

    def edit_section(self, section_id: UUID, content: SectionContent) -> None:
        with self.session.no_autoflush:
            version_id = self.session.scalar(
                select(DocumentSection.document_version_id).where(
                    DocumentSection.id == section_id
                )
            )
            if version_id is None:
                raise DraftEditRejected("Draft section not found")
            self._draft(version_id)
            section = self.session.scalar(
                select(DocumentSection)
                .where(DocumentSection.id == section_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if section is None:
                raise DraftEditRejected("Draft section not found")
            section.title = content.title
            section.text = content.text
            section.page_from = content.page_from
            section.page_to = content.page_to
            section.assistant_allowed = content.assistant_allowed
        self._audit(section_id, "document_section", "document_section.edit_draft")
