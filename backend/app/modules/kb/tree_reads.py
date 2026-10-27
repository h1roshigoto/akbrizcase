"""Public tree snapshots; every load rechecks source ACL in one SQL statement.

No session management or chat projection lives here. A missing snapshot means
the caller must hand off safely. Invalid recommendations are omitted, including
when a formerly confirmed applicability flag was accidentally left enabled.
"""

import copy
from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import cast, exists, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.modules.catalog.applicability import confirmed_evidence
from app.modules.catalog.models import CatalogItem, EquipmentModel
from app.modules.kb.access import PUBLIC_VIEWER, accessible_version
from app.modules.kb.models import Document, DocumentSection, DocumentVersion
from app.modules.kb.tree_models import (
    DiagnosticTree,
    DiagnosticTreeItem,
    DiagnosticTreeSource,
)
from app.modules.kb.tree_schema import TreeInput, validate_graph


@dataclass(frozen=True)
class TreeSnapshot:
    tree_id: UUID
    model_id: UUID
    retired: bool
    content: TreeInput


def load_tree(
    session: Session, *, key: str | None = None, tree_id: UUID | None = None
) -> TreeSnapshot | None:
    """A key selects published only; an exact pin also permits approved retired.

    Explicit PUBLIC_VIEWER prevents staff cookies from opening internal sources.
    Single-statement projections avoid ORM identity-map or cross-query ACL caches.
    """
    if (key is None) == (tree_id is None):
        raise ValueError("Select exactly one tree key or pin")
    readable_source = exists(
        select(DocumentSection.id)
        .join(
            DocumentVersion, DocumentVersion.id == DocumentSection.document_version_id
        )
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            DocumentSection.id == DiagnosticTreeSource.section_id,
            accessible_version(PUBLIC_VIEWER, exact=tree_id is not None),
            DocumentSection.assistant_allowed.is_(True),
        )
        .correlate(DiagnosticTreeSource)
    )
    unavailable = exists(
        select(DiagnosticTreeSource.tree_id)
        .where(
            DiagnosticTreeSource.tree_id == DiagnosticTree.id,
            ~readable_source,
        )
        .correlate(DiagnosticTree)
    )
    sources = (
        select(
            func.coalesce(
                func.jsonb_agg(
                    func.jsonb_build_array(
                        DiagnosticTreeSource.node_key,
                        DiagnosticTreeSource.section_id,
                    )
                ),
                cast(literal("[]"), JSONB),
            )
        )
        .where(DiagnosticTreeSource.tree_id == DiagnosticTree.id)
        .correlate(DiagnosticTree)
        .scalar_subquery()
    )
    items = (
        select(
            func.coalesce(
                func.jsonb_agg(
                    func.jsonb_build_array(
                        DiagnosticTreeItem.node_key,
                        DiagnosticTreeItem.item_id,
                        DiagnosticTreeItem.qty,
                    )
                ),
                cast(literal("[]"), JSONB),
            )
        )
        .join(CatalogItem, CatalogItem.id == DiagnosticTreeItem.item_id)
        .where(
            DiagnosticTreeItem.tree_id == DiagnosticTree.id,
            confirmed_evidence(DiagnosticTree.model_id)
            .correlate(DiagnosticTree)
            .exists(),
        )
        .correlate(DiagnosticTree)
        .scalar_subquery()
    )
    row = session.execute(
        select(
            DiagnosticTree.id,
            DiagnosticTree.model_id,
            DiagnosticTree.status,
            DiagnosticTree.key,
            DiagnosticTree.version,
            DiagnosticTree.title,
            DiagnosticTree.graph,
            EquipmentModel.code,
            sources.label("sources"),
            items.label("items"),
        )
        .join(EquipmentModel, EquipmentModel.id == DiagnosticTree.model_id)
        .where(
            DiagnosticTree.key == key
            if key is not None
            else DiagnosticTree.id == tree_id,
            DiagnosticTree.status.in_(("published", "retired"))
            if tree_id is not None
            else DiagnosticTree.status == "published",
            DiagnosticTree.expert_name.is_not(None),
            DiagnosticTree.review_reference.is_not(None),
            DiagnosticTree.published_by.is_not(None),
            DiagnosticTree.approved_at <= DiagnosticTree.published_at,
            DiagnosticTree.published_at <= func.statement_timestamp(),
            EquipmentModel.status == "published",
            ~unavailable,
        )
    ).one_or_none()
    if row is None:
        return None
    graph = copy.deepcopy(row.graph)
    try:
        nodes = graph["nodes"]
        for node in nodes.values():
            if "source_refs" in node or "recommendations" in node:
                return None
        for node_key, section_id in row.sources:
            nodes[node_key].setdefault("source_refs", []).append(section_id)
        for node_key, item_id, qty in row.items:
            nodes[node_key].setdefault("recommendations", []).append(
                {"item_id": item_id, "qty": qty}
            )
        content = TreeInput.model_validate(
            {
                "key": row.key,
                "version": row.version,
                "title": row.title,
                "model_code": row.code,
                **graph,
            }
        )
        if validate_graph(content):
            return None
    except (KeyError, TypeError, AttributeError, ValidationError):
        return None
    return TreeSnapshot(row.id, row.model_id, row.status == "retired", content)
