"""Authenticated preparation orchestration. Outer UoW alone commits.

Every prepare() and manifest finalizer exits inside the savepoint, before the
outer commit. Failure audit is committed separately from rolled-back draft work.
"""

import json
import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.auth import has_role
from app.core.staff import authenticate_operator
from app.core.staff_models import AuditEvent
from app.modules.kb.draft_service import (
    DraftDocument,
    DraftEditRejected,
    DraftEditService,
    DraftFile,
    KeyedSection,
    SectionContent,
)
from app.modules.kb.files import FilePipeline, FileRejected
from app.modules.kb.files.formats import MIMES
from app.modules.kb.files.sandbox import run_parser
from app.modules.kb.import_manifest import (
    ImportManifest,
    ImportSection,
    checked_manifest,
    operator_file,
)


@dataclass(frozen=True)
class PreparationReport:
    code: str
    section_count: int = 0
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.code in {"IMPORTED_DRAFT", "NO_OP", "READY_FOR_MANUAL_REVIEW"}


class _ExtractionIncomplete(FileRejected):
    def __init__(self, reason: str) -> None:
        super().__init__("PREPARATION_INCOMPLETE")
        self.reason = reason


def _sections(
    manifest: ImportManifest, metadata: DraftFile, descriptor: int
) -> list[KeyedSection]:
    sections = manifest.sections
    if metadata.mime == "application/pdf":
        task = "pdf-text" if manifest.preparation == "pdf-text" else "pdf-info"
        try:
            result = json.loads(run_parser(descriptor, task))
            count = result["page_count"]
            if not isinstance(count, int) or not 1 <= count <= 100:
                raise ValueError
            if task == "pdf-text":
                pages = result["pages"]
                if not isinstance(pages, list) or len(pages) != count:
                    raise ValueError
                sections = [
                    ImportSection(
                        section_key=f"page-{i:04}",
                        title=f"Page {i}",
                        page_from=i,
                        page_to=i,
                        text=value,
                        assistant_allowed=False,
                    )
                    for i, value in enumerate(pages, 1)
                    if value.strip()
                ]
                if not sections:
                    raise FileRejected("MANUAL_TEXT_REQUIRED")
        except FileRejected:
            raise
        except (ValueError, KeyError, TypeError, AttributeError):
            raise FileRejected("EXTRACTION_FAILED") from None
        if any(s.page_to is not None and s.page_to > count for s in sections):
            raise FileRejected("PAGES_INVALID")
    elif manifest.preparation != "manual" or any(
        s.page_from is not None or s.page_to is not None for s in sections
    ):
        raise FileRejected("PAGES_INVALID")
    return [
        KeyedSection(
            s.section_key,
            SectionContent(
                s.title, s.text, s.page_from, s.page_to, s.assistant_allowed
            ),
        )
        for s in sections
    ]


def run_preparation(
    session: Session,
    *,
    login: str,
    password: str,
    manifest_path: Path,
    source_path: Path | None,
    pipeline: FilePipeline,
    retain_incomplete: bool = False,
) -> PreparationReport:
    action = "kb.import" if source_path is not None else "kb.check"
    actor = authenticate_operator(session, login, password)

    def audit(code: str) -> None:
        session.add(
            AuditEvent(
                actor_id=actor.user.id if actor else None,
                action=action,
                entity_type="document_version",
                entity_id=None,
                result=code,
                details={},
            )
        )
        session.flush()

    if actor is None:
        audit("AUTHENTICATION_DENIED")
        return PreparationReport("AUTHENTICATION_DENIED")
    if not has_role(actor.user, "admin"):
        audit("FORBIDDEN")
        return PreparationReport("FORBIDDEN")
    # Scans are bounded at 45 s each. Retain the staff lock during these bounded
    # external operations without the HTTP UoW's 15 s idle timeout expiring.
    session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '180s'"))
    try:
        with session.begin_nested(), ExitStack() as stack:
            manifest = stack.enter_context(checked_manifest(manifest_path))
            document = DraftDocument(
                manifest.code, manifest.title, manifest.kind, manifest.visibility
            )
            service = DraftEditService(session, actor)
            if source_path is not None:
                extension = source_path.suffix.lower().lstrip(".")
                if extension not in MIMES:
                    raise FileRejected("FILE_EXTENSION")
                descriptor = stack.enter_context(operator_file(source_path))
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    accepted = pipeline.accept(
                        stream, filename="operator." + extension, mime=MIMES[extension]
                    )
                metadata, descriptor = stack.enter_context(
                    pipeline.prepare(accepted.file_key)
                )
                if metadata.checksum_sha256 != manifest.checksum_sha256:
                    raise FileRejected("CHECKSUM_CONFLICT")
                session.refresh(actor.user)
                if not has_role(actor.user, "admin"):
                    raise FileRejected("FORBIDDEN")
                existing = service.find_import(document, manifest.version)
                version = service.import_draft(document, manifest.version, metadata)
                if existing is not None:
                    bound, _ = stack.enter_context(pipeline.prepare(version.file_key))
                    if bound != DraftFile(
                        version.file_key,
                        version.mime,
                        version.size_bytes,
                        version.checksum_sha256,
                    ):
                        raise FileRejected("FILE_METADATA_CONFLICT")
            else:
                found = service.find_import(document, manifest.version)
                if found is None:
                    raise FileRejected("DRAFT_NOT_FOUND")
                version = found
                metadata, descriptor = stack.enter_context(
                    pipeline.prepare(version.file_key)
                )
                if (
                    metadata
                    != DraftFile(
                        version.file_key,
                        version.mime,
                        version.size_bytes,
                        manifest.checksum_sha256,
                    )
                    or version.checksum_sha256 != manifest.checksum_sha256
                ):
                    raise FileRejected("FILE_METADATA_CONFLICT")
                existing = version
            try:
                sections = _sections(manifest, metadata, descriptor)
                preparation_code = None
            except FileRejected as error:
                # Only extraction of an otherwise verified PDF can leave an empty
                # draft. Invalid manual pages/manifests always roll back entirely.
                if source_path is None or manifest.preparation != "pdf-text":
                    raise
                if not retain_incomplete:
                    # Roll back this attempt. The CLI may retain an empty draft
                    # only in a NEW outer UoW, with fresh authentication and all
                    # file checks repeated, never a half-successful transaction.
                    raise _ExtractionIncomplete(error.code) from None
                sections = []
                preparation_code = error.code
            changed = service.reconcile_sections(
                version.id, sections, check=source_path is None
            )
            code = (
                "PREPARATION_INCOMPLETE"
                if preparation_code
                else "READY_FOR_MANUAL_REVIEW"
                if source_path is None
                else "IMPORTED_DRAFT"
                if existing is None or changed
                else "NO_OP"
            )
            service.record_import(version.id, action, code)
            report = PreparationReport(code, len(sections), preparation_code)
        return report
    except _ExtractionIncomplete as error:
        audit(error.code)
        return PreparationReport(error.code, reason=error.reason)
    except (FileRejected, DraftEditRejected) as error:
        # DraftEditRejected has fixed service messages only. Map legacy messages
        # rather than ever emitting arbitrary exception text.
        code = error.code if isinstance(error, FileRejected) else "DRAFT_CONFLICT"
        if isinstance(error, DraftEditRejected) and str(error) == "CHECKSUM_CONFLICT":
            code = "CHECKSUM_CONFLICT"
        audit(code)
        return PreparationReport(code)
    except OSError:
        audit("OPERATOR_IO_FAILURE")
        return PreparationReport("OPERATOR_IO_FAILURE")
