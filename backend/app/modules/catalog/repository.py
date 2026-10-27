"""Parameterized PostgreSQL reads using the existing catalog/search indexes."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, literal, or_, select, true
from sqlalchemy.orm import Session, aliased

from app.modules.catalog.applicability import confirmed_evidence, public_applicability
from app.modules.catalog.models import CatalogItem, EquipmentModel
from app.schemas.catalog import CatalogQuery
from app.schemas.common import Pagination
from app.schemas.sources import Applicability


@dataclass(frozen=True)
class CatalogRecord:
    item: CatalogItem
    applicability: Applicability | None


class CatalogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def equipment_models(self, page: Pagination) -> tuple[list[EquipmentModel], int]:
        predicate = EquipmentModel.status == "published"
        total = self.session.scalar(
            select(func.count()).select_from(EquipmentModel).where(predicate)
        )
        items = self.session.scalars(
            select(EquipmentModel)
            .where(predicate)
            .order_by(EquipmentModel.code, EquipmentModel.id)
            .limit(page.limit)
            .offset(page.offset)
        )
        return list(items), total or 0

    def items(self, query: CatalogQuery) -> tuple[list[CatalogRecord], int]:
        predicates = [CatalogItem.status == "published"]
        if query.kind is not None:
            predicates.append(CatalogItem.kind == query.kind)
        if query.q and (term := query.q.strip()):
            # ILIKE on the indexed columns can use the existing gin_trgm_ops
            # indexes. Escape LIKE metacharacters BEFORE adding our wildcards.
            term = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{term}%"
            predicates.append(
                or_(
                    CatalogItem.name.ilike(pattern, escape="\\"),
                    CatalogItem.sku.ilike(pattern, escape="\\"),
                )
            )
        if query.model_id is not None:
            predicates.append(confirmed_evidence(query.model_id).exists())
        applicability = (
            public_applicability(query.model_id) if query.model_id else literal(None)
        )
        allowed = (
            select(CatalogItem, applicability.label("applicability"))
            .where(*predicates)
            .cte("allowed_catalog")
            .prefix_with("MATERIALIZED")
        )
        page = (
            select(allowed)
            .order_by(allowed.c.sku, allowed.c.id)
            .limit(query.limit)
            .offset(query.offset)
            .cte("catalog_page")
        )
        total = select(func.count().label("total")).select_from(allowed).cte("total")
        item = aliased(CatalogItem, page)
        rows = self.session.execute(
            select(total.c.total, item, page.c.applicability)
            .select_from(total.outerjoin(page, true()))
            .order_by(page.c.sku, page.c.id)
            .execution_options(populate_existing=True)
        ).all()
        return [
            CatalogRecord(row, Applicability.model_validate(value) if value else None)
            for _, row, value in rows
            if row is not None
        ], rows[0][0]

    def item(self, item_id: UUID, model_id: UUID | None) -> CatalogRecord | None:
        query = select(
            CatalogItem, public_applicability(model_id) if model_id else literal(None)
        ).where(CatalogItem.id == item_id, CatalogItem.status == "published")
        if model_id is not None:
            query = query.where(
                select(EquipmentModel.id)
                .where(
                    EquipmentModel.id == model_id, EquipmentModel.status == "published"
                )
                .exists()
            )
        row = self.session.execute(
            query.execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            return None
        return CatalogRecord(
            row[0], Applicability.model_validate(row[1]) if row[1] else None
        )
