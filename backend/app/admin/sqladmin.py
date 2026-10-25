"""SQLAdmin's supported auth hooks consume the request's PostgreSQL authority."""

from pathlib import Path
from typing import ClassVar

from fastapi import FastAPI, Request
from sqladmin import Admin, BaseView, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from app.admin.security import error_response, request_identity, security_settings
from app.core.audit import record_staff_event
from app.core.auth import StaffRole, has_role
from app.core.csrf import SESSION_COOKIE


class StaffAuthentication(AuthenticationBackend):
    def __init__(self) -> None:
        # AuthenticationBackend's default installs Starlette SessionMiddleware.
        # Its extension point accepts an empty middleware list: PostgreSQL is
        # our sole authority, established by StaffSecurityMiddleware each time.
        self.middlewares = []
        self.admin: Admin | None = None

    async def authenticate(self, request: Request) -> Response | bool:
        identity = request_identity(request)
        if identity is None:
            return False
        view = getattr(request.scope.get("endpoint"), "__self__", None)
        if not isinstance(view, (StaffModelView, StaffBaseView)) and self.admin:
            view = next(
                (
                    view
                    for view in self.admin.views
                    if view.identity == request.path_params.get("identity")
                ),
                None,
            )
        required = (
            view.required_role
            if isinstance(view, (StaffModelView, StaffBaseView))
            else "manager"
        )
        if not has_role(identity.user, required):
            await run_in_threadpool(
                record_staff_event,
                request.state.staff_db,
                actor_id=identity.user.id,
                action="staff.access",
                result="forbidden",
            )
            return error_response(request, 403, "FORBIDDEN", "Insufficient permissions")
        return True

    async def login(self, request: Request) -> bool:
        return False  # The explicit /admin/auth/login endpoint owns issuance.

    async def logout(self, request: Request) -> Response | bool:
        return error_response(
            request, 405, "METHOD_NOT_ALLOWED", "Use POST /admin/auth/logout"
        )


class StaffModelView(ModelView):
    """Future service-backed forms inherit this; admin is the safe default.

    MVP-011 order views set required_role='manager'. Generic CRUD registration
    is deliberately absent. Mutations must use request.state.staff_db and the
    owning business service so authorization and writes share the staff lock.
    """

    required_role: ClassVar[StaffRole] = "admin"

    def is_accessible(self, request: Request) -> bool:
        identity = request_identity(request)
        return identity is not None and has_role(identity.user, self.required_role)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)


class StaffBaseView(BaseView):
    required_role: ClassVar[StaffRole] = "admin"

    def is_accessible(self, request: Request) -> bool:
        identity = request_identity(request)
        return identity is not None and has_role(identity.user, self.required_role)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)


class StaffAdmin(Admin):
    def add_model_view(self, view: type[ModelView]) -> None:
        if not issubclass(view, StaffModelView):
            raise ValueError("Administrative forms must inherit StaffModelView")
        if any(hasattr(getattr(view, name), "_action") for name in dir(view)):
            raise ValueError("Use exposed POST handlers; SQLAdmin actions use GET")
        super().add_model_view(view)

    def add_base_view(self, view: type[BaseView]) -> None:
        if not issubclass(view, StaffBaseView):
            raise ValueError("Administrative pages must inherit StaffBaseView")
        super().add_base_view(view)


def install_sqladmin(app: FastAPI) -> Admin:
    authentication = StaffAuthentication()
    admin = StaffAdmin(
        app,
        title="Staff administration",
        authentication_backend=authentication,
        templates_dir=str(Path(__file__).parent / "templates"),
    )
    authentication.admin = admin

    def staff_csrf(request: Request) -> str:
        return security_settings(request).issue(
            request.cookies[SESSION_COOKIE], "session"
        )

    admin.templates.env.globals["staff_csrf"] = staff_csrf
    # Templates use relative same-origin URLs. Forwarded headers are never
    # authority for URL generation or CSRF validation.
    return admin
