"""Sanitized HTTP errors and server-generated canonical request identifiers."""

import secrets
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.core.http_security import TrustedProxy
from app.core.rate_limits import RateLimited
from app.schemas.common import ErrorDetail, ErrorEnvelope


def administrative_request(request: Request) -> bool:
    return any(
        request.url.path == root or request.url.path.startswith(root + "/")
        for root in ("/admin", "/api/v1/admin")
    )


def error_response(
    request: Request, status: int, code: str, message: str
) -> JSONResponse:
    identifier: UUID = request.state.request_id
    body = ErrorEnvelope(
        error=ErrorDetail(code=code, message=message, details={}),
        request_id=identifier,
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=status)


def install_api_errors(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_identifier(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request.state.request_id = uuid4()
        request.state.csp_nonce = secrets.token_urlsafe(24)
        origins = request.headers.getlist("origin")
        public_api = request.url.path.startswith(
            "/api/v1/"
        ) and not administrative_request(request)
        denied = False
        if public_api and origins:
            proxy: TrustedProxy = getattr(
                request.app.state, "trusted_proxy", TrustedProxy()
            )
            try:
                expected_origin = await run_in_threadpool(
                    proxy.external_origin, request
                )
                denied = len(origins) != 1 or origins[0] != expected_origin
            except HTTPException:
                denied = True
        if public_api and request.method not in {"GET", "HEAD", "OPTIONS"}:
            denied |= request.headers.get("sec-fetch-site") == "cross-site"
        response = (
            error_response(request, 403, "ORIGIN_DENIED", "Origin denied")
            if denied
            else await call_next(request)
        )
        response.headers["X-Request-ID"] = str(request.state.request_id)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'nonce-"
            + request.state.csp_nonce
            + "'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/v1/") or administrative_request(request):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RateLimited)
    async def rate_limited(request: Request, error: RateLimited) -> JSONResponse:
        response = error_response(request, 429, "RATE_LIMITED", "Too many requests")
        response.headers["Retry-After"] = str(error.retry_after)
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        if administrative_request(request):
            code = {
                401: "AUTHENTICATION_REQUIRED",
                403: "FORBIDDEN",
                404: "NOT_FOUND",
                405: "METHOD_NOT_ALLOWED",
                503: "AUTH_UNAVAILABLE",
            }.get(error.status_code, "REQUEST_FAILED")
            return error_response(request, error.status_code, code, "Request failed")
        code, message = {
            404: ("NOT_FOUND", "Object not found"),
            410: ("SESSION_EXPIRED", "Session expired"),
            503: ("SERVICE_UNAVAILABLE", "Service temporarily unavailable"),
        }.get(error.status_code, ("HTTP_ERROR", "Request could not be completed"))
        return error_response(request, error.status_code, code, message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        # Do not echo arbitrary input, exception context or object identifiers.
        if administrative_request(request):
            return error_response(request, 422, "INVALID_REQUEST", "Invalid request")
        return error_response(
            request, 422, "VALIDATION_ERROR", "Invalid request parameters"
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError) -> JSONResponse:
        if administrative_request(request):
            return error_response(
                request, 503, "AUTH_UNAVAILABLE", "Authentication unavailable"
            )
        return error_response(
            request, 503, "SERVICE_UNAVAILABLE", "Service temporarily unavailable"
        )
