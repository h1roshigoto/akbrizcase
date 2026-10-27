"""Controlled KB lifecycle. Only the outer UoW commits or rolls back.

Logical review leaves status=draft; immutable audit evidence binds it to the
exact file metadata and section set. All file/input finalizers precede commit.
"""

import hashlib
import json
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedStaff, has_role
from app.core.staff import authenticate_operator
from app.core.staff_models import AuditEvent
from app.modules.kb.dependencies import DependencyProvider, EquipmentManualDependencies
from app.modules.kb.draft_service import DraftFile
from app.modules.kb.files import FilePipeline, FileRejected
from app.modules.kb.files.sandbox import run_parser
from app.modules.kb.import_manifest import ImportSection, checked_record
from app.modules.kb.lifecycle_evidence import (
    ContentBinding,
    PublicReleaseEvidence,
    ReviewEvidence,
)
from app.modules.kb.models import Document, DocumentSection, DocumentVersion

Operation = Literal["review", "publish", "retire", "revoke", "visibility", "preview"]
AuditDetails = dict[str, str | bool | int]


class LifecycleRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LifecycleReport:
    code: str
    technical: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.code in {
            "REVIEWED",
            "PUBLISHED",
            "RETIRED",
            "REVOKED",
            "VISIBILITY_CHANGED",
            "PREVIEW",
            "NO_OP",
        }


def _digest(value: object) -> str:
    # Versioned canonical JSON: UTF-8, no normalization, sorted object keys,
    # compact separators. Arrays carry sorted section keys and explicit nulls.
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def section_fingerprint(sections: list[DocumentSection]) -> str:
    return _digest(
        {
            "format": "kb-sections-v1",
            "sections": [
                [
                    s.section_key,
                    s.title,
                    s.page_from,
                    s.page_to,
                    s.text,
                    s.assistant_allowed,
                ]
                for s in sorted(sections, key=lambda s: s.section_key)
            ],
        }
    )


class LifecycleService:
    """Caller authenticates under commit-scoped lock_staff, as for draft edits."""

    def __init__(
        self,
        session: Session,
        actor: AuthenticatedStaff,
        pipeline: FilePipeline,
        *,
        dependencies: tuple[DependencyProvider, ...] = (EquipmentManualDependencies(),),
    ) -> None:
        session.refresh(actor.user)
        if not has_role(actor.user, "admin"):
            raise LifecycleRejected("FORBIDDEN")
        self.session, self.actor, self.pipeline = session, actor, pipeline
        self.dependencies = dependencies

    def _audit(
        self,
        operation: Operation,
        document: Document,
        version: DocumentVersion | None,
        details: AuditDetails,
        *,
        result: str = "success",
    ) -> AuditEvent:
        event = AuditEvent(
            actor_id=self.actor.user.id,
            action="kb." + operation,
            entity_type="document_version" if version else "document",
            entity_id=version.id if version else document.id,
            result=result,
            details={"document_id": str(document.id), **details},
        )
        self.session.add(event)
        self.session.flush()
        return event

    def _locked(self, document_id: UUID) -> tuple[Document, list[DocumentVersion]]:
        document = self.session.scalar(
            select(Document)
            .where(Document.id == document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if document is None:
            raise LifecycleRejected("NOT_FOUND")
        versions = list(
            self.session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(DocumentVersion.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        return document, versions

    def _sections(self, version: DocumentVersion) -> list[DocumentSection]:
        # The parent version lock serializes section INSERT/UPDATE/DELETE via
        # MVP-019's trigger. Do not acquire a section lock in reverse trigger order.
        return list(
            self.session.scalars(
                select(DocumentSection)
                .where(DocumentSection.document_version_id == version.id)
                .order_by(DocumentSection.section_key)
                .execution_options(populate_existing=True)
            )
        )

    def _binding(self, version: DocumentVersion) -> ContentBinding:
        return ContentBinding(
            version_id=str(version.id),
            checksum_sha256=version.checksum_sha256,
            file_binding_sha256=_digest(
                {
                    "format": "kb-file-v1",
                    "file_key": version.file_key,
                    "mime": version.mime,
                    "size_bytes": version.size_bytes,
                    "checksum_sha256": version.checksum_sha256,
                }
            ),
            sections_sha256=section_fingerprint(self._sections(version)),
        )

    def _checked_file(self, version: DocumentVersion, stack: ExitStack) -> None:
        sections = self._sections(version)
        if not sections:
            raise LifecycleRejected("SECTIONS_REQUIRED")
        try:
            checked = [
                ImportSection(
                    section_key=s.section_key,
                    title=s.title,
                    text=s.text,
                    page_from=s.page_from,
                    page_to=s.page_to,
                    assistant_allowed=s.assistant_allowed,
                )
                for s in sections
            ]
        except ValidationError:
            raise LifecycleRejected("SECTIONS_INVALID") from None
        if len({s.section_key for s in checked}) != len(checked):
            raise LifecycleRejected("SECTIONS_INVALID")
        metadata, descriptor = stack.enter_context(
            self.pipeline.prepare(version.file_key)
        )
        if metadata != DraftFile(
            version.file_key, version.mime, version.size_bytes, version.checksum_sha256
        ):
            raise LifecycleRejected("FILE_METADATA_CONFLICT")
        if version.mime == "application/pdf":
            try:
                count = json.loads(run_parser(descriptor, "pdf-info"))["page_count"]
                if type(count) is not int or not 1 <= count <= 100:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise LifecycleRejected("PAGES_INVALID") from None
            if any(s.page_to is not None and s.page_to > count for s in checked):
                raise LifecycleRejected("PAGES_INVALID")
        elif any(s.page_from is not None or s.page_to is not None for s in checked):
            raise LifecycleRejected("PAGES_INVALID")
        # Keep prepare open across lifecycle writes and audit. execute() closes
        # it before the savepoint completes, and before the outer UoW commits.

    def _review(self, version: DocumentVersion) -> tuple[AuditEvent, ReviewEvidence]:
        if version.reviewed_by is None or version.approved_at is None:
            raise LifecycleRejected("REVIEW_REQUIRED")
        event = self.session.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.action == "kb.review",
                AuditEvent.result == "success",
                AuditEvent.entity_type == "document_version",
                AuditEvent.entity_id == version.id,
                AuditEvent.actor_id == version.reviewed_by,
                AuditEvent.details["approved_at"].as_string()
                == version.approved_at.isoformat(),
            )
            .order_by(AuditEvent.at.desc(), AuditEvent.id.desc())
            .limit(1)
        )
        if event is None:
            raise LifecycleRejected("REVIEW_REQUIRED")
        try:
            evidence = ReviewEvidence.model_validate(
                {
                    name: event.details[name]
                    for name in ReviewEvidence.model_fields
                    if name in event.details
                }
            )
        except (KeyError, ValidationError):
            raise LifecycleRejected("REVIEW_INVALID") from None
        if ContentBinding.model_validate(
            evidence.model_dump(include=set(ContentBinding.model_fields))
        ) != self._binding(version):
            raise LifecycleRejected("REVIEW_STALE")
        return event, evidence

    def _preview(
        self, document: Document, versions: list[DocumentVersion]
    ) -> dict[str, object]:
        entries: list[dict[str, object]] = []
        for version in versions:
            try:
                event, _ = self._review(version)
                review: str | None = str(event.id)
            except LifecycleRejected:
                review = None
            entries.append(
                {
                    **self._binding(version).model_dump(),
                    "version": version.version,
                    "status": version.status,
                    "section_count": len(self._sections(version)),
                    "review_event_id": review,
                }
            )
        return {
            "document_id": str(document.id),
            "status": document.status,
            "visibility": document.visibility,
            "versions": entries,
            "dependencies": [
                p.preview(
                    self.session, document.id, tuple(v.id for v in versions)
                ).technical()
                for p in self.dependencies
            ],
        }

    def execute(
        self,
        operation: Operation,
        document_id: UUID,
        *,
        version_id: UUID | None = None,
        evidence_path: Path | None = None,
        visibility: str | None = None,
    ) -> LifecycleReport:
        with ExitStack() as stack:
            return self._execute(
                stack,
                operation,
                document_id,
                version_id=version_id,
                evidence_path=evidence_path,
                visibility=visibility,
            )

    def _execute(
        self,
        stack: ExitStack,
        operation: Operation,
        document_id: UUID,
        *,
        version_id: UUID | None,
        evidence_path: Path | None,
        visibility: str | None,
    ) -> LifecycleReport:
        document, versions = self._locked(document_id)
        version = next((v for v in versions if v.id == version_id), None)
        if version_id is not None and version is None:
            raise LifecycleRejected("NOT_FOUND")
        if operation in {"review", "publish", "retire"} and version is None:
            raise LifecycleRejected("VERSION_REQUIRED")
        preview = self._preview(document, versions)
        if operation == "preview":
            self._audit(operation, document, version, {})
            return LifecycleReport("PREVIEW", preview)
        if document.status != "active" and not (
            operation == "revoke" and version is None
        ):
            raise LifecycleRejected("DOCUMENT_REVOKED")
        if operation == "review":
            assert version is not None
            if version.status != "draft":
                raise LifecycleRejected("DRAFT_REQUIRED")
            if evidence_path is None:
                raise LifecycleRejected("EVIDENCE_REQUIRED")
            with checked_record(evidence_path, ReviewEvidence) as evidence:
                if ContentBinding.model_validate(
                    evidence.model_dump(include=set(ContentBinding.model_fields))
                ) != self._binding(version):
                    raise LifecycleRejected("REVIEW_STALE")
                self._checked_file(version, stack)
                try:
                    _, previous = self._review(version)
                except LifecycleRejected:
                    previous = None
                if previous == evidence:
                    self._audit(operation, document, version, {}, result="no_op")
                    return LifecycleReport("NO_OP", {"before": preview})
                version.reviewed_by = self.actor.user.id
                version.approved_at = self.session.scalar(
                    select(func.clock_timestamp())
                )
                assert version.approved_at is not None
                details: AuditDetails = {
                    name: value
                    for name, value in evidence.model_dump().items()
                    if value is not None
                }
                details["approved_at"] = version.approved_at.isoformat()
                self._audit(operation, document, version, details)
            return LifecycleReport(
                "REVIEWED",
                {"document_id": str(document.id), "version_id": str(version.id)},
            )
        if operation == "publish":
            assert version is not None
            if version.status not in {"draft", "published"}:
                raise LifecycleRejected("DRAFT_REQUIRED")
            review, evidence = self._review(version)
            if (
                document.visibility == "public"
                and not evidence.full_file_public_allowed
            ):
                raise LifecycleRejected("PUBLIC_APPROVAL_REQUIRED")
            if version.status == "published":
                self._audit(operation, document, version, {}, result="no_op")
                return LifecycleReport("NO_OP", {"before": preview})
            self._checked_file(version, stack)
            for old in versions:
                if old.status == "published":
                    old.status = "retired"
                    self._audit(
                        "retire",
                        document,
                        old,
                        {
                            "from_status": "published",
                            "to_status": "retired",
                            "replacement_id": str(version.id),
                        },
                    )
            # Flush the previous edition FIRST: partial UNIQUE is immediate.
            self.session.flush()
            version.published_at = self.session.scalar(select(func.clock_timestamp()))
            version.status = "published"
            self._audit(
                operation,
                document,
                version,
                {
                    "from_status": "draft",
                    "to_status": "published",
                    "review_event_id": str(review.id),
                },
            )
            return LifecycleReport("PUBLISHED", {"before": preview})
        if operation == "retire":
            assert version is not None
            if version.status == "retired":
                self._audit(operation, document, version, {}, result="no_op")
                return LifecycleReport("NO_OP", {"before": preview})
            if version.status != "published":
                raise LifecycleRejected("PUBLISHED_REQUIRED")
            version.status = "retired"
            self._audit(
                operation,
                document,
                version,
                {"from_status": "published", "to_status": "retired"},
            )
            return LifecycleReport("RETIRED", {"before": preview})
        if operation == "revoke":
            target = version if version else document
            before = target.status
            if before == "revoked":
                self._audit(operation, document, version, {}, result="no_op")
                return LifecycleReport("NO_OP", {"before": preview})
            target.status = "revoked"
            self._audit(
                operation,
                document,
                version,
                {"from_status": before, "to_status": "revoked"},
            )
            return LifecycleReport("REVOKED", {"before": preview})
        if operation == "visibility":
            if visibility not in {"public", "internal"} or version_id is not None:
                raise LifecycleRejected("VISIBILITY_INVALID")
            if visibility == document.visibility:
                self._audit(operation, document, None, {}, result="no_op")
                return LifecycleReport("NO_OP", {"before": preview})
            before = document.visibility
            details = {"from_visibility": before, "to_visibility": visibility}
            if visibility == "public":
                if evidence_path is None:
                    raise LifecycleRejected("PUBLIC_APPROVAL_REQUIRED")
                readable = [v for v in versions if v.status in {"published", "retired"}]
                if not any(v.status == "published" for v in readable):
                    raise LifecycleRejected("PUBLISHED_REQUIRED")
                for entry in readable:
                    self._review(entry)
                with checked_record(evidence_path, PublicReleaseEvidence) as release:
                    if release.document_id != str(document.id) or sorted(
                        release.versions, key=lambda v: v.version_id
                    ) != sorted(
                        [self._binding(v) for v in readable], key=lambda v: v.version_id
                    ):
                        raise LifecycleRejected("PUBLIC_APPROVAL_STALE")
                    details["release_reference"] = release.release_reference
                    details["released_set_sha256"] = _digest(
                        [
                            v.model_dump()
                            for v in sorted(
                                release.versions, key=lambda v: v.version_id
                            )
                        ]
                    )
                    document.visibility = visibility
                    self._audit(operation, document, None, details)
            else:
                document.visibility = visibility
                self._audit(operation, document, None, details)
            return LifecycleReport("VISIBILITY_CHANGED", {"before": preview})
        raise LifecycleRejected("OPERATION_INVALID")


def run_lifecycle(
    session: Session,
    *,
    login: str,
    password: str,
    operation: Operation,
    document_id: UUID,
    pipeline: FilePipeline,
    version_id: UUID | None = None,
    evidence_path: Path | None = None,
    visibility: str | None = None,
) -> LifecycleReport:
    actor = authenticate_operator(session, login, password)

    def denied(code: str) -> LifecycleReport:
        session.add(
            AuditEvent(
                actor_id=actor.user.id if actor else None,
                action="kb." + operation,
                entity_type="document_version" if version_id else "document",
                entity_id=version_id or document_id,
                result=code,
                details={},
            )
        )
        session.flush()
        return LifecycleReport(code)

    if actor is None:
        return denied("AUTHENTICATION_DENIED")
    if not has_role(actor.user, "admin"):
        return denied("FORBIDDEN")
    # Bounded operator AV only; do not change HTTP statement/lock/idle limits.
    session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '180s'"))
    try:
        with session.begin_nested():
            return LifecycleService(session, actor, pipeline).execute(
                operation,
                document_id,
                version_id=version_id,
                evidence_path=evidence_path,
                visibility=visibility,
            )
    except (LifecycleRejected, FileRejected) as error:
        return denied(error.code)
    except OSError:
        return denied("OPERATOR_IO_FAILURE")
