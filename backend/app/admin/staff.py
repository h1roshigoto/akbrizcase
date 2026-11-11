"""Narrow staff UI over the existing commands and middleware-owned UoW."""

from typing import cast

from fastapi import Request
from sqladmin import expose
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, RedirectResponse, Response

from app.admin.forms import bounded_body, unique_fields
from app.admin.security import error_response, request_identity
from app.admin.sqladmin import StaffBaseView
from app.core.audit import StaffAction, record_staff_event
from app.core.auth import StaffRole
from app.core.csrf import SESSION_COOKIE
from app.core.staff import StaffOutcome, create_staff_as_actor, disable_staff_as_actor
from app.core.staff_models import StaffUser
from app.schemas.common import parse_uuid

STAFF_LIMIT = 100
FORM_BYTES = 16384


async def staff_fields(request: Request, allowed: set[str]) -> dict[str, str]:
    if (
        request.query_params
        or request.headers.get("content-type", "").split(";")[0]
        != "application/x-www-form-urlencoded"
    ):
        raise ValueError("Invalid form")
    await bounded_body(request, FORM_BYTES)
    # URL encoding can triple the wire size of a valid 1024-byte password.
    # Bound the wire body here; unique_fields limits decoded UTF-8 values.
    async with request.form(
        max_files=0, max_fields=6, max_part_size=FORM_BYTES
    ) as form:
        return unique_fields(form, allowed | {"csrf_token"})


async def invalid_form(
    request: Request, action: StaffAction, status: int = 422
) -> Response:
    actor = request_identity(request)
    assert actor is not None
    await run_in_threadpool(
        record_staff_event,
        request.state.staff_db,
        actor_id=actor.user.id,
        action=action,
        result="invalid_input",
    )
    return error_response(request, status, "INVALID_REQUEST", "Invalid staff fields")


def rejected(request: Request, outcome: StaffOutcome) -> Response:
    status = {
        "authentication_denied": 401,
        "forbidden": 403,
        "not_found": 404,
        "login_exists": 409,
    }.get(outcome.result, 422)
    return error_response(
        request, status, outcome.result.upper(), "Staff action rejected"
    )


class StaffView(StaffBaseView):
    name = "Staff"
    required_role = "admin"

    @expose("/staff", methods=["GET"], identity="staff")
    async def page(self, request: Request) -> Response:
        if request.query_params:
            return error_response(request, 422, "INVALID_REQUEST", "Invalid parameters")

        def read() -> dict[str, object]:
            # Project only technical display fields; no ORM password/session data
            # enters the template context. Read one extra row to show truncation.
            rows = (
                request.state.staff_db.execute(
                    select(
                        StaffUser.id,
                        StaffUser.login,
                        StaffUser.role,
                        StaffUser.is_active,
                    )
                    .order_by(StaffUser.login, StaffUser.id)
                    .limit(STAFF_LIMIT + 1)
                )
                .mappings()
                .all()
            )
            return {"rows": rows[:STAFF_LIMIT], "truncated": len(rows) > STAFF_LIMIT}

        return await self.templates.TemplateResponse(
            request, "staff/list.html", await run_in_threadpool(read)
        )

    @expose("/staff/create", methods=["POST"], identity="staff-create")
    async def post_create(self, request: Request) -> Response:
        try:
            values = await staff_fields(
                request, {"login", "role", "password", "password_confirm"}
            )
            if (
                not {"login", "role", "password", "password_confirm"} <= values.keys()
                or values["password"] != values["password_confirm"]
            ):
                raise ValueError("Invalid confirmation")
        except (ValueError, HTTPException) as error:
            status = error.status_code if isinstance(error, HTTPException) else 422
            return await invalid_form(request, "staff.create", status)
        actor = request_identity(request)
        assert actor is not None
        outcome = await run_in_threadpool(
            create_staff_as_actor,
            request.state.staff_db,
            actor=actor,
            login=values["login"],
            password=values["password"],
            role=cast(StaffRole, values["role"]),
        )
        if not outcome.ok:
            return rejected(request, outcome)
        return RedirectResponse(f"/admin/staff/{outcome.user_id}", status_code=303)

    @expose("/staff/{record}/disable", methods=["POST"], identity="staff-disable")
    async def post_disable(self, request: Request) -> Response:
        try:
            identifier = parse_uuid(request.path_params["record"])
            values = await staff_fields(request, {"confirm"})
            if values.get("confirm") != "disable":
                raise ValueError("Explicit confirmation required")
        except (ValueError, HTTPException) as error:
            status = error.status_code if isinstance(error, HTTPException) else 422
            return await invalid_form(request, "staff.disable", status)
        actor = request_identity(request)
        assert actor is not None
        outcome = await run_in_threadpool(
            disable_staff_as_actor,
            request.state.staff_db,
            actor=actor,
            user_id=identifier,
        )
        if not outcome.ok:
            return rejected(request, outcome)
        if identifier == actor.user.id:
            # The outer middleware commits before this acknowledgement is sent.
            # Do not render the authenticated layout/CSRF token after self-disable.
            response = HTMLResponse(
                '<!doctype html><html lang="en"><title>Access ended</title>'
                "<p>Your account is disabled. All your sessions have ended.</p>"
                '<a href="/admin/login">Sign in with another active account</a></html>'
            )
            response.delete_cookie(
                SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict"
            )
            return response
        return RedirectResponse(f"/admin/staff/{identifier}", status_code=303)

    @expose("/staff/{record}", methods=["GET"], identity="staff-detail")
    async def show(self, request: Request) -> Response:
        try:
            identifier = parse_uuid(request.path_params["record"])
            if request.query_params:
                raise ValueError("Invalid parameters")
        except ValueError:
            return error_response(request, 422, "INVALID_REQUEST", "Invalid identifier")

        def read() -> dict[str, object]:
            session = request.state.staff_db
            row = (
                session.execute(
                    select(
                        StaffUser.id,
                        StaffUser.login,
                        StaffUser.role,
                        StaffUser.is_active,
                    ).where(StaffUser.id == identifier)
                )
                .mappings()
                .one_or_none()
            )
            admins = session.scalar(
                select(func.count())
                .select_from(StaffUser)
                .where(StaffUser.role == "admin", StaffUser.is_active.is_(True))
            )
            actor = request_identity(request)
            assert actor is not None
            return {
                "row": row,
                "active_admins": admins,
                "is_self": actor.user.id == identifier,
            }

        context = await run_in_threadpool(read)
        if context["row"] is None:
            return error_response(request, 404, "NOT_FOUND", "Staff not found")
        return await self.templates.TemplateResponse(
            request, "staff/detail.html", context
        )
