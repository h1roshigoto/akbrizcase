"""ASGI entrypoint: uvicorn app.main:app."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.db import Database, database_lifespan

app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=database_lifespan)


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
