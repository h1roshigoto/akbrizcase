"""Public catalog HTTP adapters; one shared UoW per request."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ConfigDict
from sqlalchemy.orm import Session

from app.core.db import Database
from app.modules.catalog.repository import CatalogRepository
from app.modules.catalog.service import CatalogNotFound, CatalogService
from app.schemas.catalog import (
    CatalogItem,
    CatalogPage,
    CatalogQuery,
    EquipmentModelsPage,
)
from app.schemas.common import ErrorEnvelope, Pagination, UUIDValue


class CatalogParameters(CatalogQuery):
    # HTTP query values are strings. Reuse the DTO constraints while letting
    # Pydantic parse transport integers; the strict response DTOs are unchanged.
    model_config = ConfigDict(strict=False)


class PageParameters(Pagination):
    model_config = ConfigDict(strict=False)


router = APIRouter(
    prefix="/api/v1",
    tags=["catalog"],
    responses={422: {"model": ErrorEnvelope}, 503: {"model": ErrorEnvelope}},
)


def catalog_session(request: Request) -> Iterator[Session]:
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(status_code=503)
    with database.unit_of_work() as session:
        yield session


# Finish the UoW (including a possible commit failure) before sending HTTP 200.
CatalogSession = Annotated[Session, Depends(catalog_session, scope="function")]


@router.get("/equipment-models", response_model=EquipmentModelsPage)
def equipment_models(
    page: Annotated[PageParameters, Query()], session: CatalogSession
) -> EquipmentModelsPage:
    return CatalogService(CatalogRepository(session)).equipment_models(page)


@router.get("/catalog/items", response_model=CatalogPage)
def catalog_items(
    query: Annotated[CatalogParameters, Query()], session: CatalogSession
) -> CatalogPage:
    return CatalogService(CatalogRepository(session)).items(query)


@router.get(
    "/catalog/items/{id}",
    response_model=CatalogItem,
    responses={404: {"model": ErrorEnvelope}},
)
def catalog_item(
    id: UUIDValue,
    session: CatalogSession,
    model_id: UUIDValue | None = None,
) -> CatalogItem:
    try:
        return CatalogService(CatalogRepository(session)).item(id, model_id)
    except CatalogNotFound:
        raise HTTPException(status_code=404) from None
