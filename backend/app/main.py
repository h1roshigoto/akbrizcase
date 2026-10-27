"""ASGI entrypoint: uvicorn app.main:app."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.admin.auth import router as auth_router
from app.admin.orders import router as admin_orders_router
from app.admin.security import StaffSecurityMiddleware
from app.admin.sqladmin import install_sqladmin
from app.core.api_errors import install_api_errors
from app.core.config import settings
from app.core.db import Database, database_lifespan
from app.modules.catalog.router import router as catalog_router
from app.modules.kb.router import router as kb_router
from app.modules.orders.router import router as orders_router

app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=database_lifespan)
app.add_middleware(StaffSecurityMiddleware)
# Install last so request IDs wrap even early staff middleware denials.
install_api_errors(app)
app.include_router(catalog_router)
app.include_router(kb_router)
app.include_router(orders_router)
app.include_router(auth_router)
app.include_router(admin_orders_router)


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
