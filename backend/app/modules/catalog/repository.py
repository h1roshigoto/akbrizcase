"""Parameterized PostgreSQL reads using the existing catalog/search indexes."""

from uuid import UUID

from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session

from app.modules.catalog.models import CatalogItem, EquipmentModel
from app.schemas.catalog import CatalogQuery
from app.schemas.common import Pagination


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

    def items(self, query: CatalogQuery) -> tuple[list[CatalogItem], int]:
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
            # MVP-024 owns the future public-confirmation predicate. Until its
            # schema/services exist, no item has a publicly confirmed relation.
            predicates.append(false())
        total = self.session.scalar(
            select(func.count()).select_from(CatalogItem).where(*predicates)
        )
        items = self.session.scalars(
            select(CatalogItem)
            .where(*predicates)
            .order_by(CatalogItem.sku, CatalogItem.id)
            .limit(query.limit)
            .offset(query.offset)
        )
        return list(items), total or 0

    def item(self, item_id: UUID) -> CatalogItem | None:
        return self.session.scalar(
            select(CatalogItem).where(
                CatalogItem.id == item_id, CatalogItem.status == "published"
            )
        )

    def model_is_public(self, model_id: UUID) -> bool:
        return (
            self.session.scalar(
                select(EquipmentModel.id).where(
                    EquipmentModel.id == model_id, EquipmentModel.status == "published"
                )
            )
            is not None
        )
