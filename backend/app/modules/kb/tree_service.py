"""Admin-only draft import/check. The caller's outer UoW alone commits."""

import copy
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import JsonValue, TypeAdapter, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.auth import has_role
from app.core.staff import authenticate_operator
from app.core.staff_models import AuditEvent
from app.modules.catalog.applicability import confirmed_evidence
from app.modules.catalog.models import CatalogItem, CatalogItemModel, EquipmentModel
from app.modules.kb.access import PUBLIC_VIEWER, accessible_version
from app.modules.kb.files import FileRejected
from app.modules.kb.import_manifest import operator_file
from app.modules.kb.models import Document, DocumentSection, DocumentVersion
from app.modules.kb.tree_models import (
    DiagnosticTree,
    DiagnosticTreeItem,
    DiagnosticTreeSource,
)
from app.modules.kb.tree_schema import (
    MAX_YAML,
    Commit,
    Issue,
    TreeInput,
    TreeRejected,
    checksum,
    parse_tree,
    validate_graph,
)

Operation = Literal["import", "check"]


@dataclass(frozen=True)
class TreeReport:
    code: str
    tree_id: UUID | None = None
    issues: tuple[Issue, ...] = ()
    blockers: tuple[Issue, ...] = ()

    @property
    def ok(self) -> bool:
        return self.code in {"IMPORTED_DRAFT", "NO_OP", "STRUCTURALLY_VALID_DRAFT"}

    def technical(self) -> dict[str, JsonValue]:
        return {
            "code": self.code,
            "tree_id": str(self.tree_id) if self.tree_id else None,
            "issues": [asdict(issue) for issue in self.issues],
            "publication_blockers": [asdict(issue) for issue in self.blockers],
            "expert_approval": False,
        }


@contextmanager
def checked_yaml(path: Path) -> Iterator[tuple[TreeInput, str]]:
    with operator_file(path) as descriptor:
        data = os.read(descriptor, MAX_YAML + 1)
        tree = parse_tree(data)
        yield tree, checksum(data)
        with operator_file(path) as other:
            first, second = os.fstat(descriptor), os.fstat(other)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if (
                (first.st_dev, first.st_ino) != (second.st_dev, second.st_ino)
                or os.read(descriptor, MAX_YAML + 1) != data
                or os.read(other, MAX_YAML + 1) != data
            ):
                raise TreeRejected("YAML_CHANGED")


def _sources(tree: TreeInput) -> set[tuple[str, UUID]]:
    return {
        (key, UUID(source))
        for key, node in tree.nodes.items()
        for source in node.source_refs
    }


def _items(tree: TreeInput) -> set[tuple[str, UUID, int]]:
    return {
        (key, UUID(item.item_id), item.qty)
        for key, node in tree.nodes.items()
        for item in node.recommendations
    }


def _binding(
    tree: DiagnosticTree,
    sources: set[tuple[str, UUID]],
    items: set[tuple[str, UUID, int]],
) -> str:
    value = {
        "format": "diag-import-v1",
        "key": tree.key,
        "version": tree.version,
        "model_id": str(tree.model_id),
        "title": tree.title,
        "graph": tree.graph,
        "yaml_checksum": tree.yaml_checksum,
        "git_commit": tree.git_commit,
        "sources": sorted((key, str(identifier)) for key, identifier in sources),
        "items": sorted((key, str(identifier), qty) for key, identifier, qty in items),
    }
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _saved(session: Session, saved: DiagnosticTree) -> TreeInput:
    graph = copy.deepcopy(saved.graph)
    if set(graph) != {"start", "nodes"} or not isinstance(graph["nodes"], dict):
        raise TreeRejected("STORED_DATA_CONFLICT")
    nodes = graph["nodes"]
    for node in nodes.values():
        if (
            not isinstance(node, dict)
            or {"source_refs", "recommendations"} & node.keys()
        ):
            raise TreeRejected("STORED_DATA_CONFLICT")
    sources = set(
        session.execute(
            select(
                DiagnosticTreeSource.node_key, DiagnosticTreeSource.section_id
            ).where(DiagnosticTreeSource.tree_id == saved.id)
        ).tuples()
    )
    items = set(
        session.execute(
            select(
                DiagnosticTreeItem.node_key,
                DiagnosticTreeItem.item_id,
                DiagnosticTreeItem.qty,
            ).where(DiagnosticTreeItem.tree_id == saved.id)
        ).tuples()
    )
    for key, section_id in sorted(sources):
        node = nodes.get(key)
        if not isinstance(node, dict):
            raise TreeRejected("STORED_DATA_CONFLICT")
        refs = node.setdefault("source_refs", [])
        if not isinstance(refs, list):
            raise TreeRejected("STORED_DATA_CONFLICT")
        refs.append(str(section_id))
    for key, item_id, qty in sorted(items):
        node = nodes.get(key)
        if not isinstance(node, dict):
            raise TreeRejected("STORED_DATA_CONFLICT")
        recommendations = node.setdefault("recommendations", [])
        if not isinstance(recommendations, list):
            raise TreeRejected("STORED_DATA_CONFLICT")
        recommendations.append({"item_id": str(item_id), "qty": qty})
    model_code = session.scalar(
        select(EquipmentModel.code).where(EquipmentModel.id == saved.model_id)
    )
    try:
        tree = TreeInput.model_validate(
            {
                "key": saved.key,
                "version": saved.version,
                "model_code": model_code,
                "title": saved.title,
                **graph,
            }
        )
        TypeAdapter(Commit).validate_python(saved.git_commit)
    except ValidationError:
        raise TreeRejected("STORED_DATA_CONFLICT") from None
    issues = validate_graph(tree)
    if issues:
        raise TreeRejected("GRAPH_INVALID", issues)
    original = session.scalar(
        select(AuditEvent.id).where(
            AuditEvent.action == "tree.import",
            AuditEvent.entity_id == saved.id,
            AuditEvent.result == "IMPORTED_DRAFT",
            AuditEvent.details["binding_sha256"].as_string()
            == _binding(saved, sources, items),
        )
    )
    if original is None:
        raise TreeRejected("STORED_DATA_CONFLICT")
    return tree


def _dependencies(session: Session, tree: TreeInput) -> tuple[UUID, tuple[Issue, ...]]:
    """Lock parents first; dependency probes choose locks, never authorize content.

    Match MVP-024: documents -> versions -> model -> items -> applicability.
    Section writers already lock their version; no reverse section row lock.
    """
    source_ids = {identifier for _, identifier in _sources(tree)}
    item_ids = {identifier for _, identifier, _ in _items(tree)}
    model_id = session.scalar(
        select(EquipmentModel.id).where(EquipmentModel.code == tree.model_code)
    )
    if model_id is None:
        raise TreeRejected("MODEL_NOT_FOUND")
    if (
        set(session.scalars(select(CatalogItem.id).where(CatalogItem.id.in_(item_ids))))
        != item_ids
    ):
        raise TreeRejected("ITEM_NOT_FOUND")
    candidates = dict(
        session.execute(
            select(
                CatalogItemModel.item_id, CatalogItemModel.evidence_section_id
            ).where(
                CatalogItemModel.item_id.in_(item_ids),
                CatalogItemModel.model_id == model_id,
            )
        ).tuples()
    )
    section_ids = source_ids | set(candidates.values())
    parents = list(
        session.execute(
            select(DocumentSection.id, DocumentVersion.id, Document.id)
            .join(
                DocumentVersion,
                DocumentVersion.id == DocumentSection.document_version_id,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentSection.id.in_(section_ids))
        ).tuples()
    )
    if {p[0] for p in parents} != section_ids:
        raise TreeRejected("SOURCE_NOT_FOUND")
    for entity, ids in (
        (Document, {p[2] for p in parents}),
        (DocumentVersion, {p[1] for p in parents}),
        (EquipmentModel, {model_id}),
        (CatalogItem, item_ids),
    ):
        rows = list(
            session.scalars(
                select(entity)
                .where(entity.id.in_(ids))
                .order_by(entity.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if len(rows) != len(ids):
            raise TreeRejected("DEPENDENCIES_CHANGED")
    links = list(
        session.scalars(
            select(CatalogItemModel)
            .where(
                CatalogItemModel.item_id.in_(item_ids),
                CatalogItemModel.model_id == model_id,
            )
            .order_by(CatalogItemModel.item_id, CatalogItemModel.model_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    current = {link.item_id: link.evidence_section_id for link in links}
    current_parents = list(
        session.execute(
            select(DocumentSection.id, DocumentVersion.id, Document.id)
            .join(
                DocumentVersion,
                DocumentVersion.id == DocumentSection.document_version_id,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentSection.id.in_(section_ids))
        ).tuples()
    )
    if current != candidates or set(current_parents) != set(parents):
        raise TreeRejected("DEPENDENCIES_CHANGED")
    found_items = set(
        session.scalars(select(CatalogItem.id).where(CatalogItem.id.in_(item_ids)))
    )
    if found_items != item_ids:
        raise TreeRejected("ITEM_NOT_FOUND")
    public_sources = set(
        session.scalars(
            select(DocumentSection.id)
            .join(
                DocumentVersion,
                DocumentVersion.id == DocumentSection.document_version_id,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                DocumentSection.id.in_(source_ids),
                accessible_version(PUBLIC_VIEWER),
                DocumentSection.assistant_allowed.is_(True),
            )
        )
    )
    confirmed = set(
        session.scalars(
            select(CatalogItem.id).where(
                CatalogItem.id.in_(item_ids), confirmed_evidence(model_id).exists()
            )
        )
    )
    blockers = [Issue("EXPERT_REVIEW_REQUIRED")]
    if (
        session.scalar(
            select(EquipmentModel.status).where(EquipmentModel.id == model_id)
        )
        != "published"
    ):
        blockers.append(Issue("MODEL_NOT_PUBLIC", field="model_code"))
    blockers.extend(
        Issue("SOURCE_NOT_PUBLIC", key, "source_refs")
        for key, identifier in sorted(_sources(tree))
        if identifier not in public_sources
    )
    blockers.extend(
        Issue("APPLICABILITY_NOT_CONFIRMED", key, "recommendations")
        for key, identifier, _ in sorted(_items(tree))
        if identifier not in confirmed
    )
    return model_id, tuple(blockers)


def run_tree(
    session: Session,
    *,
    login: str,
    password: str,
    operation: Operation,
    path: Path | None = None,
    tree_id: UUID | None = None,
    git_commit: str | None = None,
) -> TreeReport:
    actor = authenticate_operator(session, login, password)

    def audit(report: TreeReport, binding: str | None = None) -> None:
        details: dict[str, JsonValue] = {
            "issue_codes": [i.code for i in report.issues],
            "blocker_codes": [i.code for i in report.blockers],
        }
        if binding is not None:
            details["binding_sha256"] = binding
        session.add(
            AuditEvent(
                actor_id=actor.user.id if actor else None,
                action="tree." + operation,
                entity_type="diag_tree",
                entity_id=report.tree_id,
                result=report.code,
                details=details,
            )
        )
        session.flush()

    if actor is None or not has_role(actor.user, "admin"):
        report = TreeReport("AUTHENTICATION_DENIED" if actor is None else "FORBIDDEN")
        audit(report)
        return report
    try:
        with session.begin_nested():
            if operation not in {"import", "check"} or (path is None) == (
                tree_id is None
            ):
                raise TreeRejected("OPERATION_INVALID")
            if operation == "import":
                if path is None:
                    raise TreeRejected("OPERATION_INVALID")
                try:
                    TypeAdapter(Commit).validate_python(git_commit)
                except ValidationError:
                    raise TreeRejected("COMMIT_INVALID") from None
            elif git_commit is not None:
                raise TreeRejected("OPERATION_INVALID")

            def execute(tree: TreeInput, digest: str | None) -> TreeReport:
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": "diag-tree:" + tree.key},
                )
                model_id, blockers = _dependencies(session, tree)
                versions = list(
                    session.scalars(
                        select(DiagnosticTree)
                        .where(DiagnosticTree.key == tree.key)
                        .order_by(DiagnosticTree.version)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                if any(version.model_id != model_id for version in versions):
                    raise TreeRejected("MODEL_CONFLICT")
                existing = next(
                    (v for v in versions if v.version == tree.version), None
                )
                if operation == "check":
                    if tree_id is not None:
                        if existing is None or existing.id != tree_id:
                            raise TreeRejected("DRAFT_NOT_FOUND")
                        if existing.status != "draft":
                            raise TreeRejected("DRAFT_REQUIRED")
                        current = _saved(session, existing)
                        if current != tree:
                            raise TreeRejected("STORED_DATA_CONFLICT")
                    result = TreeReport(
                        "STRUCTURALLY_VALID_DRAFT", tree_id, blockers=blockers
                    )
                    audit(result)
                    return result
                if existing is not None:
                    if existing.yaml_checksum != digest:
                        raise TreeRejected("CHECKSUM_CONFLICT")
                    saved = _saved(session, existing)
                    if (
                        saved.graph() != tree.graph()
                        or saved.title != tree.title
                        or _sources(saved) != _sources(tree)
                        or _items(saved) != _items(tree)
                    ):
                        raise TreeRejected("STORED_DATA_CONFLICT")
                    result = TreeReport("NO_OP", existing.id, blockers=blockers)
                    audit(result)
                    return result
                assert digest is not None and git_commit is not None
                edition = DiagnosticTree(
                    key=tree.key,
                    version=tree.version,
                    model_id=model_id,
                    title=tree.title,
                    graph=tree.graph(),
                    yaml_checksum=digest,
                    git_commit=git_commit,
                    status="draft",
                )
                session.add(edition)
                session.flush()
                for key, identifier in sorted(_sources(tree)):
                    session.add(
                        DiagnosticTreeSource(
                            tree_id=edition.id, node_key=key, section_id=identifier
                        )
                    )
                for key, identifier, qty in sorted(_items(tree)):
                    session.add(
                        DiagnosticTreeItem(
                            tree_id=edition.id,
                            node_key=key,
                            item_id=identifier,
                            qty=qty,
                        )
                    )
                session.flush()
                result = TreeReport("IMPORTED_DRAFT", edition.id, blockers=blockers)
                audit(result, _binding(edition, _sources(tree), _items(tree)))
                return result

            if path is not None:
                with checked_yaml(path) as (tree, digest):
                    report = execute(tree, digest)
            else:
                # Probe key, then lock key BEFORE dependencies or tree rows.
                saved = session.get(DiagnosticTree, tree_id, populate_existing=True)
                if saved is None:
                    raise TreeRejected("DRAFT_NOT_FOUND")
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": "diag-tree:" + saved.key},
                )
                report = execute(_saved(session, saved), None)
            session.refresh(actor.user)
            if not has_role(actor.user, "admin"):
                raise TreeRejected("FORBIDDEN")
        return report
    except (TreeRejected, FileRejected) as error:
        report = TreeReport(
            error.code, issues=(error.issues if isinstance(error, TreeRejected) else ())
        )
        audit(report)
        return report
    except OSError:
        report = TreeReport("OPERATOR_IO_FAILURE")
        audit(report)
        return report
