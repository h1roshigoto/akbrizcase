"""Public visibility and explicit projections into the existing wire DTOs."""

from uuid import UUID

from app.modules.catalog.models import CatalogItem as ItemRecord
from app.modules.catalog.repository import CatalogRepository
from app.schemas.catalog import (
    CatalogCard,
    CatalogItem,
    CatalogPage,
    CatalogQuery,
    EquipmentModel,
    EquipmentModelsPage,
)
from app.schemas.common import Pagination


class CatalogNotFound(Exception):
    """Missing and nonpublic items/models have the same public error."""


def card(record: ItemRecord) -> CatalogCard:
    # Deliberate allowlist: status, storage keys and timestamps stay internal.
    # Money/UUID validation and serialization belong to the shared DTOs.
    return CatalogCard.model_validate(
        {
            "id": record.id,
            "sku": record.sku,
            "kind": record.kind,
            "name": record.name,
            "unit": record.unit,
            "price_mode": record.price_mode,
            "price": record.price,
            "currency": record.currency,
            "tax_note": record.tax_note,
            "requires_shipping": record.requires_shipping,
            "image_url": None,  # Verified serving is introduced by MVP-025.
            "revision": record.revision,
            "applicability": None,
        }
    )


class CatalogService:
    def __init__(self, repository: CatalogRepository) -> None:
        self.repository = repository

    def equipment_models(self, page: Pagination) -> EquipmentModelsPage:
        records, total = self.repository.equipment_models(page)
        return EquipmentModelsPage(
            items=[
                EquipmentModel(
                    id=row.id, code=row.code, name=row.name, description=row.description
                )
                for row in records
            ],
            total=total,
            limit=page.limit,
            offset=page.offset,
        )

    def items(self, query: CatalogQuery) -> CatalogPage:
        records, total = self.repository.items(query)
        return CatalogPage(
            items=[
                card(row.item).model_copy(update={"applicability": row.applicability})
                for row in records
            ],
            total=total,
            limit=query.limit,
            offset=query.offset,
        )

    def item(self, item_id: UUID, model_id: UUID | None) -> CatalogItem:
        result = self.repository.item(item_id, model_id)
        if result is None:
            raise CatalogNotFound
        record = result.item
        return CatalogItem.model_validate(
            {
                **card(record).model_dump(),
                "description": record.description,
                "attributes": record.attributes,
                "applicability": result.applicability,
            }
        )
