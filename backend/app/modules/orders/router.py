"""Bounded strict input; synchronous UoW completes before emitting a receipt."""

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.contracts.canonical import parse_order_json
from app.core.api_errors import error_response
from app.core.db import Database
from app.core.http_security import ip_subjects
from app.core.rate_limits import RateLimited, check_limit
from app.modules.chat.access import ChatExpired, ChatNotFound
from app.modules.orders.privacy_service import PrivacyUnavailable, public_policy
from app.modules.orders.security import OrderSecurity
from app.modules.orders.service import (
    OrderRejected,
    create_order,
    effective_consent,
    replay_order,
)
from app.schemas.common import (
    ChatHeaders,
    ErrorEnvelope,
    OrderHeaders,
    SecretToken,
    UUIDValue,
)
from app.schemas.consent import ConsentText, PrivacyPolicy
from app.schemas.orders import OrderCreate, OrderReceipt

MAX_BODY_BYTES = 64 * 1024
router = APIRouter(
    prefix="/api/v1",
    tags=["orders"],
    responses={
        422: {"model": ErrorEnvelope},
        429: {"model": ErrorEnvelope},
        503: {"model": ErrorEnvelope},
    },
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


@router.get("/privacy-policy", response_model=PrivacyPolicy)
def privacy_policy(request: Request) -> PrivacyPolicy | JSONResponse:
    try:
        with database_for(request).unit_of_work() as session:
            bundle, collection_enabled = public_policy(session)
            return PrivacyPolicy(
                **bundle.policy.model_dump(),
                consent_version=bundle.consent.version,
                collection_enabled=collection_enabled,
                synthetic=bundle.approval_status == "SYNTHETIC ONLY",
            )
    except OrderRejected as error:
        return error_response(request, error.status, error.code, error.message)
    except PrivacyUnavailable:
        raise HTTPException(503) from None


def quota_safe_replay(
    session: Session,
    request: Request,
    body: OrderCreate,
    headers: OrderHeaders,
    security: OrderSecurity,
) -> OrderReceipt | None:
    try:
        return replay_order(session, body, headers, security)
    except OrderRejected as error:
        if error.status == 404:
            # Occupied but unauthenticated/erased/expired keys must not be
            # distinguished from unused keys by an exhausted caller's quota.
            check_limit(
                session, "order", ip_subjects(request, "order", session), consume=False
            )
        raise


def persist_order(
    request: Request,
    body: OrderCreate,
    headers: OrderHeaders,
    chat_token: str | None = None,
) -> tuple[OrderReceipt, int]:
    security: OrderSecurity | None = request.app.state.order_security
    if security is None:
        raise HTTPException(503)
    try:
        with database_for(request).unit_of_work() as session:
            subjects = ip_subjects(request, "order", session)
            receipt = quota_safe_replay(session, request, body, headers, security)
            status = 200 if receipt is not None else 201
            if receipt is None:
                try:
                    check_limit(session, "order", subjects, consume=False)
                except RateLimited:
                    # The last-slot winner may have committed after our first
                    # unused-key lookup. READ COMMITTED sees it now, without
                    # charging/denying its authenticated concurrent replays.
                    receipt = quota_safe_replay(
                        session, request, body, headers, security
                    )
                    if receipt is None:
                        raise
                    status = 200
                if receipt is None:
                    receipt = create_order(session, body, headers, security, chat_token)
                    check_limit(session, "order", subjects)
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
        receipt = quota_safe_replay(session, request, body, headers, security)
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
        410: {"model": ErrorEnvelope},
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
    description="Create or replay with source=site or owned source=chat. "
    "Chat creation requires X-Chat-Token. Maximum JSON body: 65536 bytes. "
    "Authenticated replay is available for 24 hours unless the order is erased.",
)
async def orders(
    request: Request,
    response: Response,
    idempotency_key: Annotated[UUIDValue, Header(alias="Idempotency-Key")],
    request_token: Annotated[SecretToken, Header(alias="X-Request-Token")],
    chat_token: Annotated[SecretToken | None, Header(alias="X-Chat-Token")] = None,
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
        if any(
            len(request.headers.getlist(name)) != 1
            for name in ("Idempotency-Key", "X-Request-Token")
        ):
            raise ValueError
        if body.source == "chat":
            if len(request.headers.getlist("X-Chat-Token")) != 1:
                raise ValueError
            ChatHeaders.model_validate({"X-Chat-Token": chat_token})
    except (ValueError, UnicodeError, RecursionError):
        return error_response(
            request, 422, "VALIDATION_ERROR", "Invalid request parameters"
        )
    headers = OrderHeaders.model_validate(
        {"Idempotency-Key": idempotency_key, "X-Request-Token": request_token}
    )
    try:
        receipt, response.status_code = await run_in_threadpool(
            persist_order, request, body, headers, chat_token
        )
        return receipt
    except OrderRejected as error:
        return error_response(request, error.status, error.code, error.message)
    except ChatNotFound:
        raise HTTPException(404) from None
    except ChatExpired:
        raise HTTPException(410) from None
