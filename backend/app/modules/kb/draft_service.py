"""Internal admin draft edits only. Import, publication and ACL arrive later.

The caller supplies an authenticated identity and owns the UoW. No generic field
mapping, lifecycle changes, commits, file reads or content-bearing audit entries.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import has_role
from app.core.staff_models import AuditEvent
from app.core.staff_sessions import StaffIdentity
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


class DraftEditService:
    def __init__(self, session: Session, actor: StaffIdentity) -> None:
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

    def _audit(self, identifier: UUID, entity: str, action: str) -> None:
        self.session.add(
            AuditEvent(
                actor_id=self.actor_id,
                entity_type=entity,
                entity_id=identifier,
                action=action,
                result="success",
                details={},
            )
        )
        self.session.flush()

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
