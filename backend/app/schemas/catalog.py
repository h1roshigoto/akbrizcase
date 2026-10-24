"""Public catalog cards, pagination, applicability and bounded search inputs."""

from typing import Annotated

from pydantic import Field, JsonValue, model_validator

from app.schemas.common import (
    DTO,
    CatalogKind,
    Code,
    Currency,
    Money,
    Page,
    Pagination,
    PositiveInt,
    PriceMode,
    Query,
    Text200,
    UUIDValue,
)
from app.schemas.sources import Applicability


class EquipmentModel(DTO):
    id: UUIDValue
    code: Code
    name: Text200
    description: str = Field(max_length=10000)


class CatalogQuery(Pagination):
    q: Query | None = None
    kind: CatalogKind | None = None
    model_id: UUIDValue | None = None


class CatalogCard(DTO):
    id: UUIDValue
    sku: Text200
    kind: CatalogKind
    name: Text200
    unit: Text200
    price_mode: PriceMode
    price: Money | None
    currency: Currency
    tax_note: Text200
    requires_shipping: bool
    image_url: (
        Annotated[
            str,
            Field(
                max_length=250,
                pattern=(
                    r"^/api/v1/catalog/items/[0-9a-f-]{36}/image/"
                    r"[A-Za-z0-9_-]{1,128}(\.(jpg|jpeg|png))?$"
                ),
            ),
        ]
        | None
    )
    revision: PositiveInt
    applicability: Applicability | None

    @model_validator(mode="after")
    def catalog_invariants(self) -> "CatalogCard":
        if (self.price_mode == "fixed") != (self.price is not None):
            raise ValueError("fixed requires price; on_request requires null")
        if self.kind == "service" and self.requires_shipping:
            raise ValueError("services cannot require shipping")
        if self.image_url is not None:
            prefix = f"/api/v1/catalog/items/{self.id}/image/"
            if not self.image_url.startswith(prefix):
                raise ValueError("image must belong to the current item")
        return self


class CatalogItem(CatalogCard):
    description: str = Field(max_length=10000)
    attributes: Annotated[dict[str, JsonValue], Field(max_length=200)]


class CatalogPage(Page[CatalogCard]):
    pass


class EquipmentModelsPage(Page[EquipmentModel]):
    pass
