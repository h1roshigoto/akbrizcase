"""ASGI entrypoint: uvicorn app.main:app."""

from fastapi import FastAPI

from app.core.config import settings

app = FastAPI(title=settings.app_name, debug=settings.debug)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Report process liveness without accessing external services."""
    return {"status": "ok"}
