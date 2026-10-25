"""Protected GET adapters sharing the staff transaction and order read-service."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ConfigDict, ValidationError
from sqladmin import expose
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from app.admin.security import error_response, require_role
from app.admin.sqladmin import StaffBaseView
from app.modules.orders.read_service import OrderNotFound, OrderReadService
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
            request, "orders/detail.html", {"order": detail}
        )
