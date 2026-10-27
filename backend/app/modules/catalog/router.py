"""Public catalog HTTP adapters; one shared UoW per request."""

import hashlib
import os
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import Database
from app.modules.catalog.images.pipeline import ImagePipeline, valid_key
from app.modules.catalog.models import CatalogItem as ItemRecord
from app.modules.catalog.repository import CatalogRepository
from app.modules.catalog.service import CatalogNotFound, CatalogService
from app.modules.kb.files.process import FileRejected
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


def image_pipeline() -> ImagePipeline:
    return ImagePipeline.from_environment()


@router.get(
    "/catalog/items/{id}/image/{image_key}",
    response_class=Response,
    responses={
        200: {"content": {"image/jpeg": {}, "image/png": {}}},
        404: {"model": ErrorEnvelope},
    },
)
def catalog_image(
    id: UUIDValue,
    image_key: str,
    session: CatalogSession,
    pipeline: Annotated[ImagePipeline, Depends(image_pipeline)],
) -> Response:
    def current() -> bool:
        return (
            session.scalar(
                select(ItemRecord.id).where(
                    ItemRecord.id == id,
                    ItemRecord.status == "published",
                    ItemRecord.image_key == image_key,
                )
            )
            is not None
        )

    try:
        valid_key(image_key)
        if not current():
            raise FileRejected("IMAGE_NOT_FOUND")
        with pipeline.prepare(image_key) as (metadata, descriptor):
            os.lseek(descriptor, 0, os.SEEK_SET)
            with os.fdopen(os.dup(descriptor), "rb") as snapshot:
                data = snapshot.read(metadata.size_bytes + 1)
            if len(data) != metadata.size_bytes or (
                hashlib.sha256(data).hexdigest() != metadata.checksum_sha256
            ):
                raise FileRejected("FILE_CHANGED")
        # READ COMMITTED recheck after all expensive work, with no locks that
        # could delay removal/replacement/archive. No deferred file-path reads.
        if not current():
            raise FileRejected("IMAGE_NOT_FOUND")
    except (FileRejected, OSError):
        raise HTTPException(404) from None
    return Response(
        data,
        media_type=metadata.mime,
        headers={
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )
