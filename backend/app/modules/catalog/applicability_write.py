"""Protected applicability workflow, sharing the KB lock order and outer UoW."""

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedStaff, has_role
from app.core.staff import authenticate_operator
from app.core.staff_models import AuditEvent
from app.modules.catalog.applicability_evidence import (
    ApplicabilityPublication,
    ApplicabilityReview,
    PairBinding,
)
from app.modules.catalog.applicability_mutations import bump_revision, clear_permission
from app.modules.catalog.models import CatalogItem, CatalogItemModel, EquipmentModel
from app.modules.kb.files import FileRejected
from app.modules.kb.import_manifest import checked_record
from app.modules.kb.lifecycle_service import LifecycleRejected, LifecycleReport
from app.modules.kb.models import Document, DocumentSection, DocumentVersion

Operation = Literal["candidate", "replace", "review", "publish", "revoke", "preview"]


class ApplicabilityWriteService:
    """Entry point authenticates with commit-scoped lock_staff before any locks."""

    def __init__(self, session: Session, actor: AuthenticatedStaff) -> None:
        session.refresh(actor.user)
        if not has_role(actor.user, "admin"):
            raise LifecycleRejected("FORBIDDEN")
        self.session, self.actor = session, actor

    def _locked(
        self,
        item_id: UUID,
        model_id: UUID,
        new_section: UUID | None,
    ) -> tuple[
        CatalogItem, EquipmentModel, CatalogItemModel | None, DocumentVersion, Document
    ]:
        # This unlocked probe chooses parents, never authorizes a write. A link
        # changed while waiting is rejected after locking, not followed backwards.
        previous = self.session.scalar(
            select(CatalogItemModel.evidence_section_id).where(
                CatalogItemModel.item_id == item_id,
                CatalogItemModel.model_id == model_id,
            )
        )
        sections = {s for s in (previous, new_section) if s is not None}
        if not sections:
            raise LifecycleRejected("NOT_FOUND")
        parents = self.session.execute(
            select(DocumentSection.id, DocumentVersion.id, Document.id)
            .join(
                DocumentVersion,
                DocumentVersion.id == DocumentSection.document_version_id,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentSection.id.in_(sections))
        ).all()
        if len(parents) != len(sections):
            raise LifecycleRejected("NOT_FOUND")
        documents = list(
            self.session.scalars(
                select(Document)
                .where(Document.id.in_({p[2] for p in parents}))
                .order_by(Document.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        versions = list(
            self.session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id.in_({d.id for d in documents}))
                .order_by(DocumentVersion.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        # Model writers acquire only model -> items -> links, never KB parents.
        model = self.session.scalar(
            select(EquipmentModel)
            .where(EquipmentModel.id == model_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        item = self.session.scalar(
            select(CatalogItem)
            .where(CatalogItem.id == item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if model is None or item is None:
            raise LifecycleRejected("NOT_FOUND")
        link = self.session.scalar(
            select(CatalogItemModel)
            .where(
                CatalogItemModel.item_id == item_id,
                CatalogItemModel.model_id == model_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (link.evidence_section_id if link else None) != previous:
            raise LifecycleRejected("CANDIDATE_CHANGED")
        section_id = new_section or previous
        parent = next(p for p in parents if p[0] == section_id)
        version = next(v for v in versions if v.id == parent[1])
        document = next(d for d in documents if d.id == parent[2])
        # Recheck that a draft section was not deleted during the initial probe.
        if (
            self.session.get(DocumentSection, section_id, populate_existing=True)
            is None
        ):
            raise LifecycleRejected("NOT_FOUND")
        return item, model, link, version, document

    @staticmethod
    def _binding(link: CatalogItemModel) -> dict[str, str]:
        return {
            "item_id": str(link.item_id),
            "model_id": str(link.model_id),
            "evidence_section_id": str(link.evidence_section_id),
            "expert_name": link.expert_name or "",
            "review_reference": link.review_reference or "",
            "approved_at": link.approved_at.isoformat() if link.approved_at else "",
        }

    def _review(self, link: CatalogItemModel) -> AuditEvent:
        values = self._binding(link)
        event = self.session.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.action == "applicability.review",
                AuditEvent.result == "success",
                AuditEvent.entity_id == link.item_id,
                *[AuditEvent.details[k].as_string() == v for k, v in values.items()],
            )
            .order_by(AuditEvent.at.desc(), AuditEvent.id.desc())
            .limit(1)
        )
        if event is None or link.approved_at is None:
            raise LifecycleRejected("REVIEW_REQUIRED")
        return event

    def _audit(
        self,
        operation: Operation,
        item: CatalogItem,
        link: CatalogItemModel,
        before: dict[str, str],
        was_public: bool,
        *,
        result: str = "success",
        extra: dict[str, str | bool | int] | None = None,
    ) -> AuditEvent:
        details: dict[str, str | bool | int] = {
            **self._binding(link),
            **{"before_" + k: v for k, v in before.items()},
            "before_public_allowed": was_public,
            "after_public_allowed": link.public_allowed,
            "published_by": str(link.published_by) if link.published_by else "",
            "published_at": link.published_at.isoformat() if link.published_at else "",
            "revision": item.revision,
            **(extra or {}),
        }
        event = AuditEvent(
            actor_id=self.actor.user.id,
            action="applicability." + operation,
            entity_type="catalog_item",
            entity_id=item.id,
            result=result,
            details=details,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def execute(
        self,
        operation: Operation,
        item_id: UUID,
        model_id: UUID,
        *,
        section_id: UUID | None = None,
        evidence_path: Path | None = None,
    ) -> LifecycleReport:
        if operation in {"candidate", "replace"}:
            if section_id is None:
                raise LifecycleRejected("SECTION_REQUIRED")
        elif section_id is not None:
            raise LifecycleRejected("OPERATION_INVALID")
        item, model, link, version, document = self._locked(
            item_id, model_id, section_id
        )
        if (
            operation == "candidate"
            and link is not None
            and link.evidence_section_id != section_id
        ):
            raise LifecycleRejected("CANDIDATE_EXISTS")
        if operation != "candidate" and link is None:
            raise LifecycleRejected("NOT_FOUND")
        before = (
            {
                **self._binding(link),
                "published_by": str(link.published_by) if link.published_by else "",
                "published_at": link.published_at.isoformat()
                if link.published_at
                else "",
            }
            if link
            else {}
        )
        was_public = link.public_allowed if link else False
        changed = False
        extra: dict[str, str | bool | int] = {}
        if operation == "candidate" and link is None:
            assert section_id is not None
            link = CatalogItemModel(
                item_id=item_id,
                model_id=model_id,
                evidence_section_id=section_id,
                public_allowed=False,
            )
            self.session.add(link)
            changed = True
        assert link is not None
        if operation == "replace" and section_id != link.evidence_section_id:
            assert section_id is not None
            link.evidence_section_id = section_id
            link.expert_name = link.review_reference = None
            link.approved_at = None
            clear_permission(link)
            changed = True
        elif operation == "revoke":
            changed = link.public_allowed or link.approved_at is not None
            clear_permission(link)
            link.approved_at = None
        elif operation in {"review", "publish"}:
            if document.status != "active" or version.status != "published":
                raise LifecycleRejected("EVIDENCE_UNAVAILABLE")
            if evidence_path is None:
                raise LifecycleRejected("EVIDENCE_REQUIRED")
            if operation == "review":
                with checked_record(evidence_path, ApplicabilityReview) as evidence:
                    if PairBinding.model_validate(
                        evidence.model_dump(include=set(PairBinding.model_fields))
                    ) != PairBinding(
                        item_id=str(item_id),
                        model_id=str(model_id),
                        evidence_section_id=str(link.evidence_section_id),
                    ):
                        raise LifecycleRejected("REVIEW_STALE")
                    approved = datetime.fromisoformat(evidence.approved_at)
                    changed = (
                        link.expert_name,
                        link.review_reference,
                        link.approved_at,
                    ) != (evidence.expert_name, evidence.review_reference, approved)
                    if changed:
                        link.expert_name, link.review_reference, link.approved_at = (
                            evidence.expert_name,
                            evidence.review_reference,
                            approved,
                        )
                        clear_permission(link)
                    # Audit/input exit checks must precede the caller's commit.
                    if changed:
                        bump_revision(self.session, item)
                    event = self._audit(
                        operation,
                        item,
                        link,
                        before,
                        was_public,
                        result="success" if changed else "no_op",
                    )
                return LifecycleReport(
                    "REVIEWED" if changed else "NO_OP",
                    {
                        "review_event_id": str(event.id)
                        if changed
                        else str(self._review(link).id)
                    },
                )
            with checked_record(evidence_path, ApplicabilityPublication) as release:
                event = self._review(link)
                if any(
                    getattr(release, k) != v for k, v in self._binding(link).items()
                ) or release.review_event_id != str(event.id):
                    raise LifecycleRejected("PUBLIC_APPROVAL_STALE")
                if item.status != "published" or model.status != "published":
                    raise LifecycleRejected("PUBLISHED_REQUIRED")
                changed = not link.public_allowed
                if changed:
                    link.public_allowed = True
                    link.published_by = self.actor.user.id
                    link.published_at = self.session.scalar(
                        select(func.clock_timestamp())
                    )
                    bump_revision(self.session, item)
                self._audit(
                    operation,
                    item,
                    link,
                    before,
                    was_public,
                    result="success" if changed else "no_op",
                    extra={
                        "review_event_id": str(event.id),
                        "publication_reference": release.publication_reference,
                    },
                )
            return LifecycleReport("PUBLISHED" if changed else "NO_OP")
        elif operation == "preview":
            try:
                extra["review_event_id"] = str(self._review(link).id)
            except LifecycleRejected:
                pass
            self._audit(operation, item, link, before, was_public)
            return LifecycleReport(
                "PREVIEW",
                {
                    **self._binding(link),
                    **extra,
                    "public_allowed": link.public_allowed,
                    "revision": item.revision,
                    "evidence_status": version.status,
                    "document_status": document.status,
                },
            )
        if changed:
            bump_revision(self.session, item)
        self._audit(
            operation,
            item,
            link,
            before,
            was_public,
            result="success" if changed else "no_op",
        )
        return LifecycleReport(
            {"candidate": "CANDIDATE", "replace": "REPLACED", "revoke": "REVOKED"}[
                operation
            ]
            if changed
            else "NO_OP"
        )


def run_applicability(
    session: Session,
    *,
    login: str,
    password: str,
    operation: Operation,
    item_id: UUID,
    model_id: UUID,
    section_id: UUID | None = None,
    evidence_path: Path | None = None,
) -> LifecycleReport:
    actor = authenticate_operator(session, login, password)

    def denied(code: str) -> LifecycleReport:
        session.add(
            AuditEvent(
                actor_id=actor.user.id if actor else None,
                action="applicability." + operation,
                entity_type="catalog_item",
                entity_id=item_id,
                result=code,
                details={"model_id": str(model_id)},
            )
        )
        session.flush()
        return LifecycleReport(code)

    if actor is None:
        return denied("AUTHENTICATION_DENIED")
    if not has_role(actor.user, "admin"):
        return denied("FORBIDDEN")
    try:
        with session.begin_nested():
            return ApplicabilityWriteService(session, actor).execute(
                operation,
                item_id,
                model_id,
                section_id=section_id,
                evidence_path=evidence_path,
            )
    except (LifecycleRejected, FileRejected) as error:
        return denied(error.code)
    except OSError:
        return denied("OPERATOR_IO_FAILURE")
