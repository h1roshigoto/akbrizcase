"""Narrow SQLAdmin forms. All writes use the existing staff transaction."""

import json
from uuid import UUID

from fastapi import Request
from pydantic import ConfigDict, ValidationError
from sqladmin import expose
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import RedirectResponse, Response

from app.admin.security import error_response, request_identity
from app.admin.sqladmin import StaffBaseView
from app.modules.catalog.models import CatalogItem, EquipmentModel
from app.modules.catalog.write_dto import (
    ItemCreate,
    ItemEdit,
    ItemState,
    ModelEdit,
    ModelFields,
    ModelState,
)
from app.modules.catalog.write_service import CatalogRejected, CatalogWriteService
from app.schemas.common import Pagination, parse_uuid


class PageParameters(Pagination):
    model_config = ConfigDict(strict=False)


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


async def form_values(request: Request) -> dict[str, object]:
    try:
        async with request.form(max_fields=30, max_files=0) as form:
            if any(len(form.getlist(key)) != 1 for key in form):
                raise ValueError("Duplicate field")
            values: dict[str, object] = dict(form)
    except StarletteHTTPException:
        raise ValueError("Invalid form") from None
    values.pop("csrf_token", None)  # The shared middleware checked it already.
    if "expected_revision" in values:
        revision = values["expected_revision"]
        if (
            not isinstance(revision, str)
            or not revision.isascii()
            or not revision.isdecimal()
        ):
            raise ValueError("Invalid revision")
        values["expected_revision"] = int(revision)
    if "requires_shipping" in values:
        shipping = values["requires_shipping"]
        if shipping not in ("true", "false"):
            raise ValueError("Invalid shipping flag")
        values["requires_shipping"] = shipping == "true"
    if values.get("price") == "":
        values["price"] = None
    if "attributes" in values:
        raw = values["attributes"]
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32768:
            raise ValueError("Invalid attributes")
        values["attributes"] = json.loads(raw, object_pairs_hook=unique_object)
    return values


async def catalog_list(view: StaffBaseView, request: Request, entity: str) -> Response:
    try:
        if any(
            len(request.query_params.getlist(key)) != 1 for key in request.query_params
        ):
            raise ValueError
        page = PageParameters.model_validate(dict(request.query_params))
    except (ValidationError, ValueError):
        return error_response(request, 422, "INVALID_REQUEST", "Invalid pagination")

    def read() -> tuple[list[CatalogItem] | list[EquipmentModel], int]:
        session = request.state.staff_db
        if entity == "items":
            rows = list(
                session.scalars(
                    select(CatalogItem)
                    .order_by(CatalogItem.sku, CatalogItem.id)
                    .limit(page.limit)
                    .offset(page.offset)
                )
            )
            total = session.scalar(select(func.count()).select_from(CatalogItem))
        else:
            rows = list(
                session.scalars(
                    select(EquipmentModel)
                    .order_by(EquipmentModel.code, EquipmentModel.id)
                    .limit(page.limit)
                    .offset(page.offset)
                )
            )
            total = session.scalar(select(func.count()).select_from(EquipmentModel))
        return rows, total

    rows, total = await run_in_threadpool(read)
    return await view.templates.TemplateResponse(
        request,
        "catalog/list.html",
        {"entity": entity, "rows": rows, "total": total, "page": page},
    )


class CatalogItemsView(StaffBaseView):
    name = "Catalog items"

    @expose("/catalog-items", methods=["GET"], identity="catalog-items")
    async def items_page(self, request: Request) -> Response:
        return await catalog_list(self, request, "items")


class EquipmentModelsView(StaffBaseView):
    name = "Equipment models"

    @expose("/equipment-models", methods=["GET"], identity="equipment-models")
    async def models_page(self, request: Request) -> Response:
        return await catalog_list(self, request, "models")


class CatalogEditorView(StaffBaseView):
    name = "Catalog editor"

    def is_visible(self, request: Request) -> bool:
        return False

    @expose(
        "/catalog/{entity}/{record}", methods=["GET", "POST"], identity="catalog-editor"
    )
    async def editor(self, request: Request) -> Response:
        entity, record = request.path_params["entity"], request.path_params["record"]
        if entity not in {"items", "models"}:
            return error_response(request, 404, "NOT_FOUND", "Object not found")
        try:
            identifier = None if record == "new" else parse_uuid(record)
        except ValueError:
            return error_response(request, 422, "INVALID_REQUEST", "Invalid identifier")
        if request.method == "GET":
            return await self._read(request, entity, identifier)
        try:
            values = await form_values(request)
            actor = request_identity(request)
            assert actor is not None
            service = CatalogWriteService(request.state.staff_db, actor)
            row: CatalogItem | EquipmentModel
            if entity == "items":
                if identifier is None:
                    row = await run_in_threadpool(
                        service.create_item, ItemCreate.model_validate(values)
                    )
                else:
                    row = await run_in_threadpool(
                        service.edit_item, identifier, ItemEdit.model_validate(values)
                    )
            elif identifier is None:
                row = await run_in_threadpool(
                    service.create_model, ModelFields.model_validate(values)
                )
            else:
                row = await run_in_threadpool(
                    service.edit_model, identifier, ModelEdit.model_validate(values)
                )
        except (ValidationError, ValueError, UnicodeError, RecursionError):
            return error_response(
                request, 422, "INVALID_REQUEST", "Invalid catalog fields"
            )
        except CatalogRejected as error:
            return await self._rejected(request, error)
        except SQLAlchemyError:
            return error_response(
                request, 503, "SERVICE_UNAVAILABLE", "Catalog change unavailable"
            )
        return RedirectResponse(f"/admin/catalog/{entity}/{row.id}", status_code=303)

    async def _read(
        self, request: Request, entity: str, identifier: UUID | None
    ) -> Response:
        def read() -> CatalogItem | EquipmentModel | None:
            if identifier is None:
                return None
            session: Session = request.state.staff_db
            if entity == "items":
                return session.get(CatalogItem, identifier)
            return session.get(EquipmentModel, identifier)

        row = await run_in_threadpool(read)
        if identifier is not None and row is None:
            return error_response(request, 404, "NOT_FOUND", "Object not found")
        attributes = (
            json.dumps(row.attributes, ensure_ascii=False)
            if isinstance(row, CatalogItem)
            else "{}"
        )
        return await self.templates.TemplateResponse(
            request,
            "catalog/editor.html",
            {"entity": entity, "row": row, "attributes": attributes},
        )

    async def _rejected(self, request: Request, error: CatalogRejected) -> Response:
        if error.status == 409:
            return await self.templates.TemplateResponse(
                request, "catalog/conflict.html", {}, status_code=409
            )
        return error_response(
            request,
            error.status,
            "FORBIDDEN" if error.status == 403 else "NOT_FOUND",
            error.message,
        )

    @expose(
        "/catalog/{entity}/{record}/{action}",
        methods=["POST"],
        identity="catalog-state",
    )
    async def state(self, request: Request) -> Response:
        entity, action = request.path_params["entity"], request.path_params["action"]
        if entity not in {"items", "models"} or action not in {"publish", "archive"}:
            return error_response(request, 404, "NOT_FOUND", "Object not found")
        try:
            identifier = parse_uuid(request.path_params["record"])
            values = await form_values(request)
            actor = request_identity(request)
            assert actor is not None
            service = CatalogWriteService(request.state.staff_db, actor)
            if entity == "items":
                await run_in_threadpool(
                    service.change_item_state,
                    identifier,
                    ItemState.model_validate(values),
                    action,
                )
            else:
                await run_in_threadpool(
                    service.change_model_state,
                    identifier,
                    ModelState.model_validate(values),
                    action,
                )
        except (ValidationError, ValueError, RecursionError):
            return error_response(
                request, 422, "INVALID_REQUEST", "Invalid catalog state"
            )
        except CatalogRejected as error:
            return await self._rejected(request, error)
        except SQLAlchemyError:
            return error_response(
                request, 503, "SERVICE_UNAVAILABLE", "Catalog change unavailable"
            )
        return RedirectResponse(
            f"/admin/catalog/{entity}/{identifier}", status_code=303
        )
