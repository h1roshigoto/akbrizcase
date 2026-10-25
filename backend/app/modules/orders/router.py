"""Bounded strict input; synchronous UoW completes before emitting a receipt."""

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from app.contracts.canonical import parse_order_json
from app.core.api_errors import error_response
from app.core.db import Database
from app.modules.orders.security import OrderSecurity
from app.modules.orders.service import (
    OrderRejected,
    create_order,
    effective_consent,
    replay_order,
)
from app.schemas.common import ErrorEnvelope, OrderHeaders, SecretToken, UUIDValue
from app.schemas.consent import ConsentText
from app.schemas.orders import OrderCreate, OrderReceipt

MAX_BODY_BYTES = 64 * 1024
router = APIRouter(
    prefix="/api/v1",
    tags=["orders"],
    responses={422: {"model": ErrorEnvelope}, 503: {"model": ErrorEnvelope}},
)


def database_for(request: Request) -> Database:
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(503)
    return database


@router.get("/consent-text", response_model=ConsentText)
def consent_text(request: Request) -> ConsentText | JSONResponse:
    try:
        with database_for(request).unit_of_work() as session:
            result = effective_consent(session)
        return result
    except OrderRejected as error:
        return error_response(request, error.status, error.code, error.message)


def persist_order(
    request: Request, body: OrderCreate, headers: OrderHeaders
) -> tuple[OrderReceipt, int]:
    security: OrderSecurity | None = request.app.state.order_security
    if security is None:
        raise HTTPException(503)
    try:
        with database_for(request).unit_of_work() as session:
            receipt = replay_order(session, body, headers, security)
            status = 200 if receipt is not None else 201
            if receipt is None:
                receipt = create_order(session, body, headers, security)
        return receipt, status  # Outer UoW committed, including late constraints.
    except IntegrityError as error:
        if (
            getattr(error.orig, "sqlstate", None) != "23505"
            or getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            != "uq_orders_idempotency_key"
        ):
            raise
    # The failed UoW has fully rolled back and closed before this new Session.
    with database_for(request).unit_of_work() as session:
        receipt = replay_order(session, body, headers, security)
        if receipt is None:  # Winner was deleted; never recreate in this attempt.
            raise OrderRejected(404, "NOT_FOUND", "Object not found")
    return receipt, 200


@router.post(
    "/orders",
    response_model=OrderReceipt,
    status_code=201,
    responses={
        200: {"model": OrderReceipt},
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": OrderCreate.model_json_schema(
                        ref_template=(
                            "#/paths/~1api~1v1~1orders/post/requestBody/content/"
                            "application~1json/schema/$defs/{model}"
                        )
                    )
                }
            },
        }
    },
    description="Create or replay with source=site. Maximum JSON body: 65536 bytes. "
    "Authenticated replay is available for 24 hours unless the order is erased.",
)
async def orders(
    request: Request,
    response: Response,
    idempotency_key: Annotated[UUIDValue, Header(alias="Idempotency-Key")],
    request_token: Annotated[SecretToken, Header(alias="X-Request-Token")],
) -> OrderReceipt | JSONResponse:
    raw = bytearray()
    try:
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip()
            != "application/json"
        ):
            raise ValueError
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BODY_BYTES:
                raise ValueError
            raw.extend(chunk)
        body = parse_order_json(bytes(raw).decode("utf-8"))
        if body.source != "site":
            raise ValueError
    except (ValueError, UnicodeError, RecursionError):
        return error_response(
            request, 422, "VALIDATION_ERROR", "Invalid request parameters"
        )
    headers = OrderHeaders.model_validate(
        {"Idempotency-Key": idempotency_key, "X-Request-Token": request_token}
    )
    try:
        receipt, response.status_code = await run_in_threadpool(
            persist_order, request, body, headers
        )
        return receipt
    except OrderRejected as error:
        return error_response(request, error.status, error.code, error.message)
