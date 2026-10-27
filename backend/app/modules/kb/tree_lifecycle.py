"""Authenticated edition lifecycle, reusing MVP-027 validation and lock order.

Only the caller's outer UoW commits. Restore preserves the original immutable
approval/publication attribution; its acting admin is recorded in a new audit.
"""

from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.auth import has_role
from app.core.staff import authenticate_operator
from app.core.staff_models import AuditEvent
from app.modules.kb.files import FileRejected
from app.modules.kb.import_manifest import StrictModel, checked_record
from app.modules.kb.lifecycle_evidence import Digest, Identifier, Reference
from app.modules.kb.lifecycle_service import LifecycleReport
from app.modules.kb.tree_models import DiagnosticTree
from app.modules.kb.tree_schema import TreeRejected
from app.modules.kb.tree_service import (
    _binding,
    _dependencies,
    _items,
    _saved,
    _sources,
    checked_yaml,
)

Operation = Literal["publish", "restore", "retire", "revoke", "preview"]


class TreeReviewEvidence(StrictModel):
    tree_id: Identifier
    yaml_checksum: Digest
    binding_sha256: Digest
    expert_name: Annotated[str, Field(min_length=3, max_length=200)]
    review_reference: Reference
    approved_at: str
    decision: Literal["approved"]
    content_checked: bool

    @model_validator(mode="after")
    def approval(self) -> "TreeReviewEvidence":
        if str(UUID(self.tree_id)) != self.tree_id or not self.content_checked:
            raise ValueError("REVIEW_INVALID")
        name = self.expert_name
        if name != name.strip() or any(not c.isprintable() for c in name):
            raise ValueError("EXPERT_INVALID")
        approved = datetime.fromisoformat(self.approved_at)
        if approved.tzinfo is None or approved > datetime.now(UTC):
            raise ValueError("APPROVAL_DATE_INVALID")
        return self


def run_tree_lifecycle(
    session: Session,
    *,
    login: str,
    password: str,
    operation: Operation,
    tree_id: UUID,
    path: Path | None = None,
    evidence_path: Path | None = None,
) -> LifecycleReport:
    actor = authenticate_operator(session, login, password)

    def audit(code: str, **details: object) -> LifecycleReport:
        session.add(
            AuditEvent(
                actor_id=actor.user.id if actor else None,
                action="tree." + operation,
                entity_type="diag_tree",
                entity_id=tree_id,
                result=code,
                details=details,
            )
        )
        session.flush()
        return LifecycleReport(code, {"tree_id": str(tree_id), **details})

    if actor is None or not has_role(actor.user, "admin"):
        return audit("AUTHENTICATION_DENIED" if actor is None else "FORBIDDEN")
    try:
        with session.begin_nested(), ExitStack() as stack:
            if operation not in {"publish", "restore", "retire", "revoke", "preview"}:
                raise TreeRejected("OPERATION_INVALID")
            saved = session.get(DiagnosticTree, tree_id, populate_existing=True)
            if saved is None:
                raise TreeRejected("TREE_NOT_FOUND")
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "diag-tree:" + saved.key},
            )
            # Refresh after waiting for the key. No tree row lock precedes parents.
            session.refresh(saved)
            before = saved.status
            if operation in {"publish", "restore", "preview"}:
                if operation != "preview" and before == "revoked":
                    raise TreeRejected("TREE_REVOKED")
                tree = _saved(session, saved)
                model_id, blockers = _dependencies(session, tree)
            else:
                # Security revocation must work even with broken dependencies.
                model_id, blockers = saved.model_id, ()
            versions = list(
                session.scalars(
                    select(DiagnosticTree)
                    .where(DiagnosticTree.key == saved.key)
                    .order_by(DiagnosticTree.version)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            if any(v.model_id != model_id for v in versions):
                raise TreeRejected("MODEL_CONFLICT")
            if operation == "preview":
                return audit(
                    "PREVIEW",
                    status=saved.status,
                    yaml_checksum=saved.yaml_checksum,
                    binding_sha256=_binding(saved, _sources(tree), _items(tree)),
                    publication_blockers=[b.code for b in blockers],
                    source_count=len(_sources(tree)),
                    recommendation_count=len(_items(tree)),
                    source_ids=sorted({str(i) for _, i in _sources(tree)}),
                    item_ids=sorted({str(i) for _, i, _ in _items(tree)}),
                    model_id=str(model_id),
                    published_ids=[
                        str(v.id) for v in versions if v.status == "published"
                    ],
                )
            if operation in {"revoke", "retire"}:
                target = "revoked" if operation == "revoke" else "retired"
                if before == target:
                    return audit("NO_OP", from_status=before, to_status=target)
                if operation == "retire" and before != "published":
                    raise TreeRejected("PUBLISHED_REQUIRED")
                saved.status = target
                return audit(target.upper(), from_status=before, to_status=target)
            if operation == "publish" and before not in {"draft", "published"}:
                raise TreeRejected("DRAFT_REQUIRED")
            if operation == "restore" and before != "retired":
                raise TreeRejected("RETIRED_REQUIRED")
            if path is None:
                raise TreeRejected("YAML_REQUIRED")
            supplied, digest = stack.enter_context(checked_yaml(path))
            if digest != saved.yaml_checksum or supplied != tree:
                raise TreeRejected("YAML_CONFLICT")
            # Reconstruct once more under all parent and tree locks, and bind to
            # the immutable import audit. Probes never authorize publication.
            current = _saved(session, saved)
            if current != tree:
                raise TreeRejected("DEPENDENCIES_CHANGED")
            blocked = tuple(b for b in blockers if b.code != "EXPERT_REVIEW_REQUIRED")
            if blocked:
                raise TreeRejected("PUBLICATION_BLOCKED", blocked)
            binding = _binding(saved, _sources(current), _items(current))
            approval: tuple[str, str, datetime, datetime] | None = None
            if before == "draft":
                if evidence_path is None:
                    raise TreeRejected("REVIEW_REQUIRED")
                evidence = stack.enter_context(
                    checked_record(evidence_path, TreeReviewEvidence)
                )
                if (
                    evidence.tree_id != str(saved.id)
                    or evidence.yaml_checksum != saved.yaml_checksum
                    or evidence.binding_sha256 != binding
                ):
                    raise TreeRejected("REVIEW_STALE")
                approved = datetime.fromisoformat(evidence.approved_at).astimezone(UTC)
                now = session.scalar(select(func.clock_timestamp()))
                assert isinstance(now, datetime)
                if approved > now:
                    raise TreeRejected("APPROVAL_DATE_INVALID")
                approval = (
                    evidence.expert_name,
                    evidence.review_reference,
                    approved,
                    now,
                )
            else:
                if evidence_path is not None:
                    repeated = stack.enter_context(
                        checked_record(evidence_path, TreeReviewEvidence)
                    )
                    if (
                        repeated.tree_id != str(saved.id)
                        or repeated.yaml_checksum != saved.yaml_checksum
                        or repeated.binding_sha256 != binding
                        or repeated.expert_name != saved.expert_name
                        or repeated.review_reference != saved.review_reference
                        or datetime.fromisoformat(repeated.approved_at)
                        != saved.approved_at
                    ):
                        raise TreeRejected("REVIEW_IMMUTABLE")
                # A historical row relabelled by SQL is not reviewed content.
                original = session.scalar(
                    select(AuditEvent.id).where(
                        AuditEvent.action == "tree.publish",
                        AuditEvent.entity_id == saved.id,
                        AuditEvent.result == "PUBLISHED",
                        AuditEvent.actor_id == saved.published_by,
                        AuditEvent.details["binding_sha256"].as_string() == binding,
                        AuditEvent.details["expert_name"].as_string()
                        == saved.expert_name,
                        AuditEvent.details["review_reference"].as_string()
                        == saved.review_reference,
                        AuditEvent.details["approved_at"].as_string()
                        == (saved.approved_at.isoformat() if saved.approved_at else ""),
                        AuditEvent.details["published_at"].as_string()
                        == (
                            saved.published_at.isoformat() if saved.published_at else ""
                        ),
                    )
                )
                if original is None:
                    raise TreeRejected("REVIEW_REQUIRED")
            for old in versions:
                if old.id != saved.id and old.status == "published":
                    old.status = "retired"
                    session.add(
                        AuditEvent(
                            actor_id=actor.user.id,
                            action="tree.retire",
                            entity_type="diag_tree",
                            entity_id=old.id,
                            result="RETIRED",
                            details={"replacement_id": str(saved.id)},
                        )
                    )
            # Immediate partial UNIQUE: retire first, publish second, one UoW.
            session.flush()
            if approval is not None:
                (
                    saved.expert_name,
                    saved.review_reference,
                    saved.approved_at,
                    saved.published_at,
                ) = approval
                saved.published_by = actor.user.id
            saved.status = "published"
            session.refresh(actor.user)
            if not has_role(actor.user, "admin"):
                raise TreeRejected("FORBIDDEN")
            assert saved.approved_at is not None
            return audit(
                "NO_OP" if before == "published" else "PUBLISHED",
                from_status=before,
                to_status="published",
                binding_sha256=binding,
                expert_name=saved.expert_name,
                review_reference=saved.review_reference,
                approved_at=saved.approved_at.isoformat(),
                published_by=str(saved.published_by),
                published_at=saved.published_at.isoformat()
                if saved.published_at
                else None,
            )
    except (TreeRejected, FileRejected) as error:
        return audit(
            error.code,
            issue_codes=[i.code for i in error.issues]
            if isinstance(error, TreeRejected)
            else [],
        )
    except OSError:
        return audit("OPERATOR_IO_FAILURE")
