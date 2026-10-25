"""Actual PostgreSQL HTTP/SQLAdmin security, including direct form requests."""

import json
import re
import secrets
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from typing import Annotated
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from httpx2 import Response
from itsdangerous import TimestampSigner
from sqladmin import expose
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import Connection
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse

from app.admin.auth import router
from app.admin.security import StaffSecurityMiddleware, require_role
from app.admin.sqladmin import StaffBaseView, StaffModelView, install_sqladmin
from app.core.auth import hash_password
from app.core.csrf import LOGIN_COOKIE, SESSION_COOKIE, StaffSecurity
from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff import bootstrap_admin, create_staff, disable_staff, lock_staff
from app.core.staff_models import AuditEvent, StaffSession, StaffUser
from app.core.staff_sessions import StaffIdentity, resolve_session, token_hash
from app.main import http_error

pytestmark = pytest.mark.integration
ORIGIN = "https://localhost:8443"
PASSWORD = "synthetic-http-individual-password"


class AdminProbe(StaffBaseView):
    name = "Administrative security probe"

    @expose("/security-admin-probe", methods=["GET", "POST"])
    async def probe(self, request: Request) -> PlainTextResponse:
        return PlainTextResponse("authorized admin")


class ManagerProbe(StaffBaseView):
    name = "Manager security probe"
    required_role = "manager"

    @expose("/security-manager-probe", methods=["GET", "POST"])
    async def probe_manager(self, request: Request) -> PlainTextResponse:
        return PlainTextResponse("authorized manager")


class FormProbe(StaffModelView, model=StaffUser):
    # Test-only view: never registered in the production app. Disallow writes
    # to staff tables; form GET and denied POST exercise real SQLAdmin routing.
    column_list = [StaffUser.login]
    form_columns = [StaffUser.login]
    can_delete = False
    can_edit = False


@pytest.fixture
def http_app(staff_database: Database) -> FastAPI:
    with staff_database.unit_of_work() as session:
        assert bootstrap_admin(session, login="http-admin", password=PASSWORD).ok
        assert create_staff(
            session,
            admin_login="http-admin",
            admin_password=PASSWORD,
            login="http-manager",
            password=PASSWORD,
            role="manager",
        ).ok
    app = FastAPI()
    app.state.database = staff_database
    app.state.staff_security = StaffSecurity(frozenset({ORIGIN}), secrets.token_hex(32))
    app.exception_handler(HTTPException)(http_error)
    app.add_middleware(StaffSecurityMiddleware)
    app.include_router(router)

    @app.post("/api/v1/admin/security-probe")
    def api_probe(
        identity: Annotated[StaffIdentity, Depends(require_role("admin"))],
    ) -> dict[str, str]:
        return {"role": identity.user.role}

    admin = install_sqladmin(app)
    admin.session_maker = staff_database.sessions
    admin.is_async = False
    admin.add_view(AdminProbe)
    admin.add_view(ManagerProbe)
    admin.add_view(FormProbe)
    return app


@pytest.fixture
def client(http_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(http_app, base_url=ORIGIN, follow_redirects=False) as browser:
        yield browser


def login(
    client: TestClient, role: str = "manager", password: str = PASSWORD
) -> Response:
    client.cookies.clear()
    csrf = client.get("/admin/auth/csrf").json()["csrf_token"]
    return client.post(
        "/admin/auth/login",
        json={"login": f"http-{role}", "password": password},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )


def assert_error(response: Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"]["code"] == code
    assert body["error"]["details"] == {}
    UUID(body["request_id"])
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_login_database_hash_cookie_and_no_sliding_expiry(
    client: TestClient,
    staff_database: Database,
    role: str,
) -> None:
    before = utc_now()
    response = login(client, role)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=/" in cookie and "Domain=" not in cookie
    assert "Max-Age=28800" in cookie
    token = client.cookies[SESSION_COOKIE]
    assert len(token) == 64 and LOGIN_COOKIE not in client.cookies
    with staff_database.unit_of_work() as session:
        row = session.scalars(select(StaffSession)).one()
        assert row.token_hash == token_hash(token) and row.token_hash != token
        expiry = row.expires_at
        assert before + timedelta(hours=8) <= expiry <= utc_now() + timedelta(hours=8)
        assert expiry.utcoffset() == timedelta(0)
    assert client.get("/admin/").status_code == 200
    assert client.get("/admin/security-manager-probe").status_code == 200
    assert client.get("/admin/security-admin-probe").status_code == (
        200 if role == "admin" else 403
    )
    with staff_database.unit_of_work() as session:
        assert session.scalars(select(StaffSession)).one().expires_at == expiry


def test_non_disclosing_failures(client: TestClient, staff_database: Database) -> None:
    wrong = login(client, password="incorrect-synthetic-password")
    unknown = login(client, role="unknown")
    with staff_database.unit_of_work() as session:
        user = session.scalars(
            select(StaffUser).where(StaffUser.role == "manager")
        ).one()
        user.is_active = False
    disabled = login(client)
    for response in (wrong, unknown, disabled):
        assert_error(response, 401, "AUTHENTICATION_FAILED")
        assert response.json()["error"] == wrong.json()["error"]
        assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    with staff_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(StaffSession)) == 0


def test_browser_form_login_and_logout(client: TestClient) -> None:
    page = client.get("/admin/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert token is not None
    response = client.post(
        "/admin/auth/login",
        data={
            "login": "http-manager",
            "password": PASSWORD,
            "csrf_token": token[1],
        },
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 303, response.text
    page = client.get("/admin/")
    assert page.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert csrf is not None
    response = client.post(
        "/admin/auth/logout", data={"csrf_token": csrf[1]}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 303
    assert client.get("/admin/").status_code == 401


@pytest.mark.parametrize("cookie", ["", "forged", "f" * 64])
def test_anonymous_forged_and_missing_cookie(client: TestClient, cookie: str) -> None:
    if cookie:
        client.cookies.set(SESSION_COOKIE, cookie)
    for path in (
        "/admin/",
        "/admin/staff-user/list",
        "/admin/security-admin-probe",
        "/admin/security-manager-probe",
    ):
        assert_error(client.get(path), 401, "AUTHENTICATION_REQUIRED")


def test_rbac_direct_sqladmin_forms_and_http_dependencies(client: TestClient) -> None:
    response = login(client)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}
    for path in ("/admin/security-admin-probe", "/admin/staff-user/create"):
        assert_error(
            client.post(path, data={"login": "attempt"}, headers=headers),
            403,
            "FORBIDDEN",
        )
    assert client.get("/admin/staff-user/create").status_code == 403
    assert_error(
        client.post("/api/v1/admin/security-probe", headers=headers), 403, "FORBIDDEN"
    )
    assert (
        client.post("/admin/security-manager-probe", headers=headers).status_code == 200
    )


def test_admin_direct_requests_and_form_csrf(client: TestClient) -> None:
    token = login(client, "admin").json()["csrf_token"]
    assert client.get("/admin/staff-user/create").status_code == 200
    assert (
        client.post(
            "/api/v1/admin/security-probe",
            headers={"Origin": ORIGIN, "X-CSRF-Token": token},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/admin/security-admin-probe",
            data={"csrf_token": token},
            headers={"Origin": ORIGIN},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/admin/security-admin-probe",
            files={"csrf_token": (None, token)},
            headers={"Origin": ORIGIN},
        ).status_code
        == 200
    )
    assert_error(
        client.post(
            "/admin/staff-user/create",
            data={"login": "bypass"},
            headers={"Origin": ORIGIN},
        ),
        403,
        "CSRF_DENIED",
    )


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "https://foreign.test",
        "http://localhost:8443",
        ORIGIN + "/",
        ORIGIN + " https://foreign.test",
    ],
)
def test_login_origin_protection(client: TestClient, origin: str | None) -> None:
    csrf = client.get("/admin/auth/csrf").json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    if origin is not None:
        headers["Origin"] = origin
    assert_error(
        client.post(
            "/admin/auth/login",
            json={"login": "http-admin", "password": PASSWORD},
            headers=headers,
        ),
        403,
        "ORIGIN_DENIED",
    )


def test_login_pre_auth_csrf_and_cross_browser(
    client: TestClient, http_app: FastAPI
) -> None:
    payload = {"login": "http-admin", "password": PASSWORD}
    assert_error(
        client.post("/admin/auth/login", json=payload, headers={"Origin": ORIGIN}),
        403,
        "CSRF_DENIED",
    )

    token = client.get("/admin/auth/csrf").json()["csrf_token"]
    with TestClient(http_app, base_url=ORIGIN) as other:
        other.get("/admin/auth/csrf")
        assert_error(
            other.post(
                "/admin/auth/login",
                json=payload,
                headers={"Origin": ORIGIN, "X-CSRF-Token": token},
            ),
            403,
            "CSRF_DENIED",
        )
    assert_error(
        client.post(
            "/admin/auth/login",
            json=payload,
            headers={"Origin": ORIGIN, "X-CSRF-Token": token + "x"},
        ),
        403,
        "CSRF_DENIED",
    )


def test_expired_login_csrf_is_rejected(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = client.get("/admin/auth/csrf").json()["csrf_token"]
    timestamp = TimestampSigner.get_timestamp
    monkeypatch.setattr(
        TimestampSigner, "get_timestamp", lambda self: timestamp(self) + 601
    )
    assert_error(
        client.post(
            "/admin/auth/login",
            json={"login": "http-admin", "password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": token},
        ),
        403,
        "CSRF_DENIED",
    )


def test_malformed_login_form_fails_csrf_closed(client: TestClient) -> None:
    client.get("/admin/auth/csrf")
    assert_error(
        client.post(
            "/admin/auth/login",
            content="malformed multipart form",
            headers={"Origin": ORIGIN, "Content-Type": "multipart/form-data"},
        ),
        403,
        "CSRF_DENIED",
    )


def test_session_csrf_missing_invalid_cross_session_and_foreign_origin(
    client: TestClient, http_app: FastAPI
) -> None:
    first = login(client).json()["csrf_token"]
    with TestClient(http_app, base_url=ORIGIN) as other:
        second = login(other).json()["csrf_token"]
    for token in ("", "forged", second):
        assert_error(
            client.post(
                "/admin/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": token}
            ),
            403,
            "CSRF_DENIED",
        )
    assert_error(
        client.post(
            "/admin/auth/logout",
            headers={"Origin": "https://foreign.test", "X-CSRF-Token": first},
        ),
        403,
        "ORIGIN_DENIED",
    )
    assert client.get("/admin/").status_code == 200


def test_logout_revoke_expire_and_replay(
    client: TestClient, staff_database: Database
) -> None:
    csrf = login(client).json()["csrf_token"]
    token = client.cookies[SESSION_COOKIE]
    assert client.get("/admin/logout").status_code == 405
    response = client.post(
        "/admin/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}
    )
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert SESSION_COOKIE not in client.cookies
    client.cookies.set(SESSION_COOKIE, token)
    assert_error(client.get("/admin/"), 401, "AUTHENTICATION_REQUIRED")
    with staff_database.unit_of_work() as session:
        assert session.scalars(select(StaffSession)).one().revoked_at is not None


def test_expiry_alteration_disable_and_current_role(
    client: TestClient, staff_database: Database
) -> None:
    login(client, "admin")
    original = client.cookies[SESSION_COOKIE]
    client.cookies.clear()
    client.cookies.set(
        SESSION_COOKIE, ("a" if original[0] != "a" else "b") + original[1:]
    )
    assert client.get("/admin/").status_code == 401
    client.cookies.set(SESSION_COOKIE, original)
    with staff_database.unit_of_work() as session:
        user = session.scalars(select(StaffUser).where(StaffUser.role == "admin")).one()
        user.role = "manager"
    assert client.get("/admin/security-admin-probe").status_code == 403
    assert client.get("/admin/security-manager-probe").status_code == 200
    with staff_database.unit_of_work() as session:
        row = session.scalars(select(StaffSession)).one()
        row.expires_at = utc_now() - timedelta(microseconds=1)
    assert client.get("/admin/").status_code == 401
    login(client)
    with staff_database.unit_of_work() as session:
        user = session.scalars(
            select(StaffUser).where(StaffUser.login == "http-manager")
        ).one()
        user.is_active = False  # Even without revocation, current DB status wins.
    assert client.get("/admin/").status_code == 401


def test_session_creation_rollback_and_no_leaks(
    client: TestClient,
    staff_database: Database,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_session(mapper: object, connection: object, target: StaffSession) -> None:
        target.token_hash = "invalid"  # Real PostgreSQL CHECK failure.

    event.listen(StaffSession, "before_insert", fail_session)
    try:
        response = login(client)
        assert_error(response, 503, "AUTH_UNAVAILABLE")
        assert SESSION_COOKIE not in client.cookies
    finally:
        event.remove(StaffSession, "before_insert", fail_session)
    with staff_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(StaffSession)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "staff.login")
            )
            == 0
        )
    token = login(client).json()["csrf_token"]
    raw = client.cookies[SESSION_COOKIE]
    client.post("/admin/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": token})
    with staff_database.unit_of_work() as session:
        audit = json.dumps(
            [
                {"action": row.action, "result": row.result, "metadata": row.details}
                for row in session.scalars(select(AuditEvent))
            ]
        )
        assert "staff.login" in audit and "staff.logout" in audit
    output = capsys.readouterr()
    for secret in (PASSWORD, token, raw, SESSION_COOKIE):
        assert secret not in caplog.text + output.out + output.err + audit


def test_lookup_failure_fails_closed(client: TestClient, http_app: FastAPI) -> None:
    login(client)
    http_app.state.database = None
    assert_error(client.get("/admin/"), 503, "AUTH_UNAVAILABLE")


def test_postgresql_lookup_error_fails_closed(
    client: TestClient,
    databases: tuple[Database, Database],
) -> None:
    login(client)
    _, migrator = databases
    try:
        with migrator.engine.begin() as connection:
            connection.execute(
                text("REVOKE SELECT ON app.staff_sessions FROM akbrizcase_runtime")
            )
        assert_error(client.get("/admin/"), 503, "AUTH_UNAVAILABLE")
    finally:
        with migrator.engine.begin() as connection:
            connection.execute(
                text("GRANT SELECT ON app.staff_sessions TO akbrizcase_runtime")
            )


def test_audit_failure_rolls_back_inserted_http_session(
    client: TestClient,
    staff_database: Database,
) -> None:
    def fail_audit(mapper: object, connection: Connection, target: AuditEvent) -> None:
        if target.action == "staff.login":
            connection.exec_driver_sql("SELECT 1 / 0")

    event.listen(AuditEvent, "before_insert", fail_audit)
    try:
        assert_error(login(client), 503, "AUTH_UNAVAILABLE")
        assert SESSION_COOKIE not in client.cookies
    finally:
        event.remove(AuditEvent, "before_insert", fail_audit)
    with staff_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(StaffSession)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "staff.login")
            )
            == 0
        )


def test_concurrent_disable_blocks_request_before_authority_lookup(
    client: TestClient, staff_database: Database
) -> None:
    login(client)
    with staff_database.unit_of_work() as session:
        user = session.scalars(
            select(StaffUser).where(StaffUser.role == "manager")
        ).one()
        identifier = user.id
    locked, release, requested = Event(), Event(), Event()

    def disable() -> None:
        with staff_database.unit_of_work() as session:
            lock_staff(session)
            locked.set()
            assert release.wait(2)
            assert disable_staff(
                session,
                admin_login="http-admin",
                admin_password=PASSWORD,
                user_id=identifier,
            ).ok

    def request() -> int:
        requested.set()
        return client.get("/admin/security-manager-probe").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        disabling = pool.submit(disable)
        assert locked.wait(2)
        pending = pool.submit(request)
        assert requested.wait(2)
        release.set()
        disabling.result(timeout=5)
        assert pending.result(timeout=5) == 401


def test_reused_orm_session_refreshes_authority(staff_database: Database) -> None:
    raw = secrets.token_hex(32)
    with staff_database.unit_of_work() as session:
        user = StaffUser(
            login="http-stale", password_hash=hash_password(PASSWORD), role="admin"
        )
        session.add(user)
        session.flush()
        session.add(
            StaffSession(
                user_id=user.id,
                token_hash=token_hash(raw),
                expires_at=utc_now() + timedelta(hours=8),
            )
        )
    with staff_database.sessions() as session:
        lock_staff(session)
        first = resolve_session(session, raw)
        assert first is not None and first.user.role == "admin"
        session.commit()
        with staff_database.unit_of_work() as another:
            lock_staff(another)
            another.execute(
                text(
                    "UPDATE app.staff_users SET role='manager' WHERE login='http-stale'"
                )
            )
        lock_staff(session)
        second = resolve_session(session, raw)
        assert second is not None and second.user.role == "manager"
