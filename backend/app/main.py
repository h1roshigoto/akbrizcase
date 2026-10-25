"""ASGI entrypoint: uvicorn app.main:app."""

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response

from app.admin.auth import router as auth_router
from app.admin.security import StaffSecurityMiddleware, error_response
from app.admin.sqladmin import install_sqladmin
from app.core.config import settings
from app.core.db import Database, database_lifespan

app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=database_lifespan)
app.add_middleware(StaffSecurityMiddleware)
app.include_router(auth_router)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> Response:
    if not request.url.path.startswith(("/admin", "/api/v1/admin")):
        return await http_exception_handler(request, exc)
    codes = {
        401: "AUTHENTICATION_REQUIRED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        503: "AUTH_UNAVAILABLE",
    }
    return error_response(
        exc.status_code, codes.get(exc.status_code, "REQUEST_FAILED"), "Request failed"
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> Response:
    if not request.url.path.startswith(("/admin", "/api/v1/admin")):
        return await request_validation_exception_handler(request, exc)
    # Pydantic error input may contain passwords, cookies or arbitrary bodies.
    return error_response(422, "INVALID_REQUEST", "Invalid request")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Report process liveness without accessing external services."""
    return {"status": "ok"}


@app.get("/readyz", response_model=None)
def readyz(request: Request) -> JSONResponse:
    """Accept traffic only with an accessible, compatible PostgreSQL schema."""
    database: Database | None = request.app.state.database
    ready = database is not None and database.ready()
    return JSONResponse(
        {"status": "ok" if ready else "unavailable"},
        status_code=200 if ready else 503,
        headers={"Cache-Control": "no-store"},
    )


admin = install_sqladmin(app)
