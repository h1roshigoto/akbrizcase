"""The single confirmed SQL predicate and public projection for all consumers.

No browser credentials enter this module. Disclosure of a fact is independent
of disclosure of its evidence; SourceRef uses MVP-022's PUBLIC_VIEWER ACL.
"""

from uuid import UUID

from sqlalchemy import Select, SQLColumnExpression, case, cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.elements import ColumnElement

from app.core.staff_models import StaffUser
from app.modules.catalog.models import CatalogItem, CatalogItemModel, EquipmentModel
from app.modules.kb.access import PUBLIC_VIEWER, accessible_version
from app.modules.kb.models import Document, DocumentSection, DocumentVersion


def confirmed_evidence(
    model_id: UUID | SQLColumnExpression[UUID],
) -> Select[tuple[UUID]]:
    """Correlated to CatalogItem; EXISTS is safe before count/limit/offset.

    A publisher FK is historical attribution. Disabling that user later does
    not erase the decision; only an active admin can make it in the first place.
    Evidence status is deliberately checked on EVERY read, independently of ACL.
    """
    link = CatalogItemModel
    return (
        select(link.evidence_section_id)
        .select_from(link)
        .join(EquipmentModel, EquipmentModel.id == link.model_id)
        .join(DocumentSection, DocumentSection.id == link.evidence_section_id)
        .join(
            DocumentVersion, DocumentVersion.id == DocumentSection.document_version_id
        )
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(StaffUser, StaffUser.id == link.published_by)
        .where(
            link.item_id == CatalogItem.id,
            link.model_id == model_id,
            CatalogItem.status == "published",
            EquipmentModel.status == "published",
            link.public_allowed.is_(True),
            link.published_by.is_not(None),
            link.published_at.is_not(None),
            func.isfinite(link.published_at),
            link.published_at <= func.statement_timestamp(),
            link.expert_name.is_not(None),
            func.length(link.expert_name).between(3, 200),
            link.expert_name == func.btrim(link.expert_name),
            link.expert_name.op("!~")("[[:cntrl:]]"),
            link.review_reference.is_not(None),
            link.review_reference.op("~")("^[A-Za-z0-9][A-Za-z0-9_.:-]{2,199}$"),
            link.approved_at.is_not(None),
            func.isfinite(link.approved_at),
            link.approved_at <= link.published_at,
            DocumentVersion.status == "published",
            Document.status == "active",
        )
        .correlate(CatalogItem)
    )


def public_applicability(model_id: UUID) -> ColumnElement[object]:
    empty = cast(literal("[]"), JSONB)
    # Equivalent to KnowledgeReadService.get_source(), in the SAME statement as
    # status/count/page. No file_key, text or internal identifier is projected.
    source = func.jsonb_build_object(
        "section_id",
        DocumentSection.id,
        "document_id",
        Document.id,
        "document_version_id",
        DocumentVersion.id,
        "title",
        Document.title,
        "section",
        DocumentSection.title,
        "page_from",
        DocumentSection.page_from,
        "page_to",
        DocumentSection.page_to,
        "url",
        func.concat(
            "/api/v1/documents/", Document.id, "/versions/", DocumentVersion.id, "/file"
        ),
    )
    sources = case(
        (
            accessible_version(PUBLIC_VIEWER, exact=True)
            & DocumentSection.assistant_allowed.is_(True),
            func.jsonb_build_array(source),
        ),
        else_=empty,
    )
    confirmed = (
        confirmed_evidence(model_id)
        .with_only_columns(
            func.jsonb_build_object(
                "model_id", str(model_id), "status", "confirmed", "sources", sources
            )
        )
        .scalar_subquery()
    )
    return func.coalesce(
        confirmed,
        func.jsonb_build_object(
            "model_id", str(model_id), "status", "unknown", "sources", empty
        ),
        type_=JSONB,
    )
