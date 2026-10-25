"""Sanitized HTTP errors and server-generated canonical request identifiers."""

from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.schemas.common import ErrorDetail, ErrorEnvelope


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
        response = await call_next(request)
        response.headers["X-Request-ID"] = str(request.state.request_id)
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        code, message = {
            404: ("NOT_FOUND", "Object not found"),
            503: ("SERVICE_UNAVAILABLE", "Service temporarily unavailable"),
        }.get(error.status_code, ("HTTP_ERROR", "Request could not be completed"))
        return error_response(request, error.status_code, code, message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        # Do not echo arbitrary input, exception context or object identifiers.
        return error_response(
            request, 422, "VALIDATION_ERROR", "Invalid request parameters"
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError) -> JSONResponse:
        return error_response(
            request, 503, "SERVICE_UNAVAILABLE", "Service temporarily unavailable"
        )
