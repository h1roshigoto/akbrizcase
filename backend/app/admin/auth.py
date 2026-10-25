"""Same identity for JSON authentication and the SQLAdmin browser login."""

import secrets
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import Field, SecretStr, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.admin.security import error_response, request_identity, security_settings
from app.core.csrf import LOGIN_COOKIE, LOGIN_SECONDS, SESSION_COOKIE
from app.core.db import Database
from app.core.staff_sessions import (
    SESSION_SECONDS,
    IssuedSession,
    login_staff,
    logout_staff,
)
from app.schemas.common import DTO

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class LoginRequest(DTO):
    login: Annotated[str, Field(min_length=1, max_length=64)]
    password: SecretStr = Field(repr=False, min_length=1, max_length=1024)


def set_cookie(response: Response, name: str, value: str, seconds: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=seconds,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )


def expire_cookie(response: Response, name: str) -> None:
    response.delete_cookie(
        name, path="/", secure=True, httponly=True, samesite="strict"
    )


@router.get("/login", response_model=None)
def login_page(request: Request) -> Response:
    security = security_settings(request)
    nonce = secrets.token_hex(32)
    response = templates.TemplateResponse(
        request, "login.html", {"csrf_token": security.issue(nonce, "login")}
    )
    set_cookie(response, LOGIN_COOKIE, nonce, LOGIN_SECONDS)
    return response


@router.get("/auth/csrf")
def csrf_token(request: Request) -> JSONResponse:
    security = security_settings(request)
    identity = request_identity(request)
    binding = request.cookies[SESSION_COOKIE] if identity else secrets.token_hex(32)
    response = JSONResponse(
        {"csrf_token": security.issue(binding, "session" if identity else "login")}
    )
    if identity is None:
        set_cookie(response, LOGIN_COOKIE, binding, LOGIN_SECONDS)
    return response


@router.post("/auth/login", response_model=None)
async def login(request: Request) -> Response:
    is_json = (
        request.headers.get("content-type", "").split(";")[0] == "application/json"
    )
    try:
        if is_json:
            credentials = LoginRequest.model_validate(await request.json())
        else:
            async with request.form() as form:
                # CSRF is transport metadata, not part of the credentials DTO.
                values = dict(form)
                values.pop("csrf_token", None)
                credentials = LoginRequest.model_validate(values)
    except (ValidationError, ValueError):
        return error_response(
            request, 422, "INVALID_REQUEST", "Invalid authentication request"
        )
    database: Database | None = request.app.state.database
    if database is None:
        return error_response(
            request, 503, "AUTH_UNAVAILABLE", "Authentication unavailable"
        )

    def create() -> IssuedSession | None:
        with database.unit_of_work() as session:
            return login_staff(
                session, credentials.login, credentials.password.get_secret_value()
            )

    issued = await run_in_threadpool(create)
    if issued is None:
        return error_response(
            request, 401, "AUTHENTICATION_FAILED", "Invalid credentials"
        )
    security = security_settings(request)
    response: Response
    if is_json:
        response = JSONResponse(
            {
                "csrf_token": security.issue(issued.token, "session"),
                "expires_at": issued.identity.session.expires_at.isoformat(),
            }
        )
    else:
        response = RedirectResponse("/admin/", status_code=303)
    set_cookie(response, SESSION_COOKIE, issued.token, SESSION_SECONDS)
    expire_cookie(response, LOGIN_COOKIE)
    return response


@router.post("/auth/logout", response_model=None)
def logout(request: Request) -> Response:
    identity = request_identity(request)
    if identity is None:
        raise HTTPException(401, "AUTHENTICATION_REQUIRED")
    logout_staff(request.state.staff_db, identity)
    response: Response = (
        RedirectResponse("/admin/login", status_code=303)
        if request.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded"
        )
        else Response(status_code=204)
    )
    expire_cookie(response, SESSION_COOKIE)
    expire_cookie(response, LOGIN_COOKIE)
    return response
