"""Central administrative boundary; retain the staff lock until commit.

SQLAdmin and future /api/v1/admin handlers share request.state.staff_db. All
administrative mutations, including unregistered/direct form URLs, pass CSRF.
Only exact image replace/preview routes close initial authorization before file
processing; the image service refreshes authority in the final binding UoW.
"""

import re
from collections.abc import Callable, Iterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from app.admin.forms import bounded_body, revision, unique_fields
from app.core.api_errors import error_response as api_error_response
from app.core.audit import StaffResult, record_staff_event
from app.core.auth import StaffRole, has_role
from app.core.csrf import LOGIN_COOKIE, LOGIN_SECONDS, SESSION_COOKIE, StaffSecurity
from app.core.db import Database
from app.core.staff import lock_staff
from app.core.staff_sessions import SESSION_SECONDS, StaffIdentity, resolve_session
from app.modules.catalog.images.pipeline import MAX_BYTES
from app.modules.catalog.models import CatalogItem

IMAGE_REPLACE = re.compile(
    r"/admin/catalog/items/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}/image/replace\Z"
)
IMAGE_PREVIEW = re.compile(IMAGE_REPLACE.pattern.replace("/replace", "/preview"))


def error_response(
    request: Request, status: int, code: str, message: str
) -> JSONResponse:
    response = api_error_response(request, status, code, message)
    response.headers["Cache-Control"] = "no-store"
    return response


def security_settings(request: Request) -> StaffSecurity:
    security: StaffSecurity | None = getattr(request.state, "staff_security", None)
    if security is None:
        raise HTTPException(503, "SECURITY_UNAVAILABLE")
    return security


def request_identity(request: Request) -> StaffIdentity | None:
    identity: StaffIdentity | None = getattr(request.state, "staff_identity", None)
    return identity


def staff_transaction(request: Request) -> Iterator[Session]:
    """Reusable FastAPI dependency, also for protected non-admin read APIs.

    Under /admin the middleware owns commit before delivering the response.
    Elsewhere FastAPI owns the dependency lifetime; writes belong under /admin.
    """
    existing: Session | None = getattr(request.state, "staff_db", None)
    if existing is not None:
        yield existing
        return
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(503, "AUTH_UNAVAILABLE")
    try:
        with database.unit_of_work() as session:
            lock_staff(session)
            request.state.staff_identity = resolve_session(
                session, request.cookies.get(SESSION_COOKIE, "")
            )
            request.state.staff_db = session
            yield session
    except SQLAlchemyError:
        raise HTTPException(503, "AUTH_UNAVAILABLE") from None


def require_role(required: StaffRole) -> Callable[..., StaffIdentity]:
    """Use with Depends and staff_transaction on new administrative endpoints."""

    def authorize(
        request: Request,
        session: Annotated[Session, Depends(staff_transaction)],
    ) -> StaffIdentity:
        identity = request_identity(request)
        if identity is None:
            raise HTTPException(401, "AUTHENTICATION_REQUIRED")
        if not has_role(identity.user, required):
            record_staff_event(
                session,
                actor_id=identity.user.id,
                action="staff.access",
                result="forbidden",
            )
            raise HTTPException(403, "FORBIDDEN")
        return identity

    return authorize


class StaffSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        if path.startswith("/admin/statics/") and request.method in {"GET", "HEAD"}:
            # SQLAdmin's package-owned software assets contain no user data.
            # StaticFiles confines reads to that dependency directory. Serving
            # them without DB authorization also avoids an audit/lookup bypass
            # through Nginx's static-resource exemption. Mutations stay protected.
            return await call_next(request)
        if not any(
            path == root or path.startswith(root + "/")
            for root in ("/admin", "/api/v1/admin")
        ):
            return await call_next(request)
        try:
            response = await self._dispatch_admin(request, call_next)
        except (SQLAlchemyError, OSError):
            # Never log exception text/SQL/parameters or return a partial login.
            response = error_response(
                request, 503, "AUTH_UNAVAILABLE", "Authentication unavailable"
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # no-referrer makes Chromium send Origin: null for ordinary form POSTs.
        # same-origin preserves the explicit Origin check without cross-site refs.
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    async def _dispatch_admin(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        security: StaffSecurity | None = request.app.state.staff_security
        if security is None:
            return error_response(
                request, 503, "SECURITY_UNAVAILABLE", "Authentication unavailable"
            )
        request.state.staff_security = security
        # Only configured HTTPS authorities may generate admin URLs. This uses
        # the explicit allowlist, never client-controlled forwarding headers.
        if "https://" + request.headers.get("host", "") not in security.trusted_origins:
            return error_response(request, 403, "ORIGIN_DENIED", "Origin denied")
        request.scope["scheme"] = "https"
        path, method = request.url.path, request.method
        unsafe = method not in ("GET", "HEAD", "OPTIONS")
        if path == "/admin/logout" or (path == "/admin/login" and unsafe):
            return error_response(
                request,
                405,
                "METHOD_NOT_ALLOWED",
                "Use the protected authentication endpoints",
            )
        preauth = (
            (path == "/admin/login" and method in ("GET", "HEAD"))
            or (path == "/admin/auth/login" and method == "POST")
            or (
                path == "/admin/auth/csrf"
                and method == "GET"
                and SESSION_COOKIE not in request.cookies
            )
        )
        if unsafe:
            origin = request.headers.get("origin", "")
            if (
                origin not in security.trusted_origins
                or origin != "https://" + request.headers.get("host", "")
            ):
                await self._anonymous_denial(request, "origin_denied")
                return error_response(request, 403, "ORIGIN_DENIED", "Origin denied")
        if preauth:
            if unsafe and not await self._valid_csrf(request, security, login=True):
                await self._anonymous_denial(request, "csrf_denied")
                return error_response(
                    request, 403, "CSRF_DENIED", "CSRF verification failed"
                )
            return await call_next(request)
        database: Database | None = request.app.state.database
        if database is None:
            return error_response(
                request, 503, "AUTH_UNAVAILABLE", "Authentication unavailable"
            )
        if (method == "POST" and IMAGE_REPLACE.fullmatch(path)) or (
            method == "GET" and IMAGE_PREVIEW.fullmatch(path)
        ):
            return await self._image_upload(request, call_next, database, security)
        if unsafe and re.fullmatch(
            r"/admin/catalog/items/[^/]+/(?:applicability/[^/]+|image/remove)", path
        ):
            try:
                await bounded_body(request, 32768)
            except StarletteHTTPException as error:
                return error_response(
                    request, error.status_code, "INVALID_REQUEST", "Invalid form body"
                )
        uow = database.unit_of_work()
        session = await run_in_threadpool(uow.__enter__)
        response: Response
        try:
            await run_in_threadpool(lock_staff, session)
            identity = await run_in_threadpool(
                resolve_session, session, request.cookies.get(SESSION_COOKIE, "")
            )
            request.state.staff_identity = identity
            request.state.staff_db = session
            if identity is None:
                await run_in_threadpool(
                    record_staff_event,
                    session,
                    actor_id=None,
                    action="staff.access",
                    result="authentication_denied",
                )
                response = error_response(
                    request, 401, "AUTHENTICATION_REQUIRED", "Authentication required"
                )
            elif unsafe and not await self._valid_csrf(request, security, login=False):
                await run_in_threadpool(
                    record_staff_event,
                    session,
                    actor_id=identity.user.id,
                    action="staff.csrf",
                    result="csrf_denied",
                )
                response = error_response(
                    request, 403, "CSRF_DENIED", "CSRF verification failed"
                )
            else:
                response = await call_next(request)
        except BaseException as exc:
            await run_in_threadpool(uow.__exit__, type(exc), exc, exc.__traceback__)
            raise
        else:
            # Commit must succeed before Set-Cookie or protected data is sent.
            await run_in_threadpool(uow.__exit__, None, None, None)
            return response

    async def _image_upload(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
        database: Database,
        security: StaffSecurity,
    ) -> Response:
        """Only exact image replace/preview routes get two-stage transactions.

        Initial authority uses the existing resolver/lock. Its UoW closes before
        receiving bytes. The fresh binding UoW executes no SQL until the image
        service has completed AV/Pillow and rechecks active user/session itself.
        All other routes retain the commit-scoped global staff lock.
        """

        def authenticate() -> StaffIdentity | None:
            with database.unit_of_work() as session:
                lock_staff(session)
                identity = resolve_session(
                    session, request.cookies.get(SESSION_COOKIE, "")
                )
                if identity is None or not has_role(identity.user, "admin"):
                    record_staff_event(
                        session,
                        actor_id=identity.user.id if identity else None,
                        action="staff.access",
                        result="forbidden" if identity else "authentication_denied",
                    )
                elif request.method == "GET":
                    row = session.get(CatalogItem, UUID(request.url.path.split("/")[4]))
                    request.state.image_preview_key = row.image_key if row else None
                return identity

        identity = await run_in_threadpool(authenticate)
        if identity is None:
            return error_response(
                request, 401, "AUTHENTICATION_REQUIRED", "Authentication required"
            )
        if not has_role(identity.user, "admin"):
            return error_response(request, 403, "FORBIDDEN", "Insufficient permissions")
        request.state.staff_identity = identity

        async def proceed() -> Response:
            uow = database.unit_of_work()
            request.state.staff_db = await run_in_threadpool(uow.__enter__)
            try:
                response = await call_next(request)
            except BaseException as exc:
                await run_in_threadpool(uow.__exit__, type(exc), exc, exc.__traceback__)
                raise
            else:
                await run_in_threadpool(uow.__exit__, None, None, None)
                return response

        if request.method == "GET":
            return await proceed()

        async def csrf_denied() -> Response:
            def record() -> None:
                with database.unit_of_work() as session:
                    record_staff_event(
                        session,
                        actor_id=identity.user.id,
                        action="staff.csrf",
                        result="csrf_denied",
                    )

            await run_in_threadpool(record)
            return error_response(
                request, 403, "CSRF_DENIED", "CSRF verification failed"
            )

        try:
            await bounded_body(request, MAX_BYTES + 16384)
            if (
                request.query_params
                or request.headers.get("content-type", "").split(";")[0]
                != "multipart/form-data"
            ):
                raise ValueError("Invalid multipart")
            async with request.form(
                max_files=1, max_fields=3, max_part_size=1024
            ) as form:
                upload = form.get("image")
                if len(form.getlist("csrf_token")) != 1:
                    return await csrf_denied()
                values = unique_fields(
                    type(form)([(k, v) for k, v in form.multi_items() if k != "image"]),
                    {"csrf_token", "expected_revision"},
                )
                tokens = request.headers.getlist("x-csrf-token")
                token = values.get("csrf_token", "")
                if (
                    len(tokens) > 1
                    or (tokens and tokens[0] != token)
                    or not security.verify(
                        token,
                        request.cookies.get(SESSION_COOKIE, ""),
                        "session",
                        SESSION_SECONDS,
                    )
                ):
                    return await csrf_denied()
                if len(form.getlist("image")) != 1 or not isinstance(
                    upload, UploadFile
                ):
                    raise ValueError("One image required")
                if upload.size is None or not 0 < upload.size <= MAX_BYTES:
                    raise StarletteHTTPException(413, "Image too large")
                request.state.image_revision = revision(
                    values.get("expected_revision", "")
                )
                request.state.image_upload = upload
                return await proceed()
        except StarletteHTTPException as error:
            return error_response(
                request, error.status_code, "INVALID_REQUEST", "Invalid image form"
            )
        except ValueError:
            return error_response(request, 422, "INVALID_REQUEST", "Invalid image form")

    async def _valid_csrf(
        self, request: Request, security: StaffSecurity, *, login: bool
    ) -> bool:
        token = request.headers.get("x-csrf-token", "")
        if not token and request.headers.get("content-type", "").split(";")[0] in (
            "application/x-www-form-urlencoded",
            "multipart/form-data",
        ):
            # Cache body so downstream SQLAdmin receives the same form stream.
            try:
                await bounded_body(request, 6 * 1024 * 1024)
                async with request.form(max_fields=100, max_files=1) as form:
                    values = form.getlist("csrf_token")
                    if len(values) == 1 and isinstance(values[0], str):
                        token = values[0]
            except (ValueError, StarletteHTTPException):
                return False
        return security.verify(
            token,
            request.cookies.get(LOGIN_COOKIE if login else SESSION_COOKIE, ""),
            "login" if login else "session",
            LOGIN_SECONDS if login else SESSION_SECONDS,
        )

    async def _anonymous_denial(self, request: Request, result: StaffResult) -> None:
        database: Database | None = request.app.state.database
        if database is not None:

            def record() -> None:
                with database.unit_of_work() as session:
                    record_staff_event(
                        session, actor_id=None, action="staff.csrf", result=result
                    )

            await run_in_threadpool(record)
