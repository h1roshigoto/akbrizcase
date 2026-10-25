"""Protected order projections and narrow service-backed status adapters."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ConfigDict, ValidationError
from sqladmin import expose
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from app.admin.security import error_response, request_identity, require_role
from app.admin.sqladmin import StaffBaseView
from app.modules.orders.read_service import OrderNotFound, OrderReadService
from app.modules.orders.service import OrderRejected
from app.modules.orders.status_service import TRANSITIONS, change_status
from app.schemas.admin import OrderStatusChange, OrderStatusResult
from app.schemas.common import ErrorEnvelope, UUIDValue, parse_uuid
from app.schemas.order_reads import OrderDetail, OrderPage, OrderReadQuery


class OrderParameters(OrderReadQuery):
    model_config = ConfigDict(strict=False)


router = APIRouter(
    prefix="/api/v1/admin/orders",
    tags=["staff orders"],
    dependencies=[Depends(require_role("manager"))],
    responses={status: {"model": ErrorEnvelope} for status in (401, 403, 422, 503)},
)


@router.get("", response_model=OrderPage)
def orders(
    request: Request,
    query: Annotated[OrderParameters, Query()],
) -> OrderPage:
    return OrderReadService(request.state.staff_db).list(query)


@router.get(
    "/{id}", response_model=OrderDetail, responses={404: {"model": ErrorEnvelope}}
)
def order(request: Request, id: UUIDValue) -> OrderDetail:
    try:
        return OrderReadService(request.state.staff_db).detail(id)
    except OrderNotFound:
        raise HTTPException(404) from None


@router.patch(
    "/{id}/status",
    response_model=OrderStatusResult,
    responses={status: {"model": ErrorEnvelope} for status in (404, 409)},
)
def order_status(
    request: Request, id: UUIDValue, change: OrderStatusChange
) -> OrderStatusResult | Response:
    actor = request_identity(request)
    assert actor is not None  # require_role and the middleware established it.
    try:
        return change_status(request.state.staff_db, id, change, actor)
    except OrderRejected as error:
        return error_response(request, error.status, error.code, error.message)
    except SQLAlchemyError:
        return error_response(
            request, 503, "SERVICE_UNAVAILABLE", "Status change unavailable"
        )


class OrdersView(StaffBaseView):
    name = "Orders"
    required_role = "manager"

    @expose("/orders", methods=["GET"], identity="orders")
    async def orders_page(self, request: Request) -> Response:
        try:
            if any(
                len(request.query_params.getlist(key)) != 1
                for key in request.query_params
            ):
                raise ValueError
            query = OrderParameters.model_validate(dict(request.query_params))
        except (ValidationError, ValueError):
            return error_response(request, 422, "INVALID_REQUEST", "Invalid request")
        page = await run_in_threadpool(
            OrderReadService(request.state.staff_db).list, query
        )
        return await self.templates.TemplateResponse(
            request, "orders/list.html", {"page": page, "status": query.status}
        )


class OrderDetailView(StaffBaseView):
    name = "Order details"
    required_role = "manager"

    def is_visible(self, request: Request) -> bool:
        return False

    @expose("/orders/{id}", methods=["GET"], identity="order-detail")
    async def order_page(self, request: Request) -> Response:
        try:
            identifier = parse_uuid(request.path_params["id"])
        except ValueError:
            return error_response(request, 422, "INVALID_REQUEST", "Invalid request")
        try:
            detail = await run_in_threadpool(
                OrderReadService(request.state.staff_db).detail, identifier
            )
        except OrderNotFound:
            return error_response(request, 404, "NOT_FOUND", "Object not found")
        return await self.templates.TemplateResponse(
            request,
            "orders/detail.html",
            {"order": detail, "next_statuses": TRANSITIONS[detail.status]},
        )

    @expose("/orders/{id}/status", methods=["POST"], identity="order-status")
    async def status_page(self, request: Request) -> Response:
        try:
            identifier = parse_uuid(request.path_params["id"])
            async with request.form() as form:
                if any(len(form.getlist(key)) != 1 for key in form):
                    raise ValueError
                values: dict[str, object] = dict(form)
                values.pop("csrf_token", None)  # Already checked by middleware.
                version = values.get("expected_version")
                if (
                    not isinstance(version, str)
                    or not version.isascii()
                    or not version.isdecimal()
                ):
                    raise ValueError
                values["expected_version"] = int(version)
                if values.get("reason") == "":
                    values["reason"] = None
                change = OrderStatusChange.model_validate(values)
        except (ValidationError, ValueError):
            return error_response(
                request, 422, "INVALID_REQUEST", "Invalid status or reason"
            )
        actor = request_identity(request)
        assert actor is not None
        try:
            await run_in_threadpool(
                change_status, request.state.staff_db, identifier, change, actor
            )
        except OrderRejected as error:
            if error.status == 409:
                return await self.templates.TemplateResponse(
                    request,
                    "orders/conflict.html",
                    {"order_id": identifier},
                    status_code=409,
                )
            return error_response(request, error.status, error.code, error.message)
        except SQLAlchemyError:
            # Handle before SQLAdmin's mounted application's 500 response starts.
            # The shared service's savepoint has already rolled back all writes.
            return error_response(
                request, 503, "SERVICE_UNAVAILABLE", "Status change unavailable"
            )
        return RedirectResponse(f"/admin/orders/{identifier}", status_code=303)
