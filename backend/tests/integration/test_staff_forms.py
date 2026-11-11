"""MVP-052 acceptance in the existing PostgreSQL 16 core suite."""

import json
import re
import secrets
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import event, func, select
from sqlalchemy.engine import Connection

from app.admin.auth import router
from app.admin.security import StaffSecurityMiddleware
from app.admin.sqladmin import install_sqladmin
from app.core.api_errors import install_api_errors
from app.core.auth import StaffRole, hash_password, verify_password
from app.core.csrf import SESSION_COOKIE, StaffSecurity
from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff import (
    authenticate_operator,
    bootstrap_admin,
    create_staff,
    create_staff_as_actor,
    disable_staff,
    disable_staff_as_actor,
    lock_staff,
)
from app.core.staff_models import AuditEvent, StaffSession, StaffUser
from app.core.staff_sessions import login_staff, resolve_session

pytestmark = pytest.mark.integration
ORIGIN = "https://localhost:8443"
PASSWORD = "synthetic-staff-forms-password"
NEW_PASSWORD = " synthetic-new-individual-password "


@pytest.fixture
def forms_app(staff_database: Database) -> FastAPI:
    with staff_database.unit_of_work() as session:
        assert bootstrap_admin(session, login="forms-admin", password=PASSWORD).ok
        assert create_staff(
            session,
            admin_login="forms-admin",
            admin_password=PASSWORD,
            login="forms-manager",
            password=PASSWORD,
            role="manager",
        ).ok
    app = FastAPI()
    app.state.database = staff_database
    app.state.staff_security = StaffSecurity(frozenset({ORIGIN}), secrets.token_hex(32))
    app.add_middleware(StaffSecurityMiddleware)
    install_api_errors(app)
    app.include_router(router)
    install_sqladmin(app)
    return app


@pytest.fixture
def browser(forms_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(forms_app, base_url=ORIGIN, follow_redirects=False) as client:
        yield client


def login(
    client: TestClient, login: str = "forms-admin", password: str = PASSWORD
) -> Response:
    client.cookies.clear()
    csrf = client.get("/admin/auth/csrf").json()["csrf_token"]
    return client.post(
        "/admin/auth/login",
        json={"login": login, "password": password},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )


def create_form(**overrides: str) -> dict[str, str]:
    return {
        "login": "forms-new",
        "password": NEW_PASSWORD,
        "password_confirm": NEW_PASSWORD,
        "role": "manager",
        **overrides,
    }


def identifier(database: Database, login: str) -> UUID:
    with database.unit_of_work() as session:
        result = session.scalar(select(StaffUser.id).where(StaffUser.login == login))
        assert result is not None
        return result


def headers(csrf: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_browser_create_and_cli_postconditions(
    browser: TestClient, staff_database: Database, role: StaffRole
) -> None:
    assert login(browser).status_code == 200
    page = browser.get("/admin/staff")
    assert page.status_code == 200
    assert f'href="{ORIGIN}/admin/staff"' in page.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert csrf is not None
    response = browser.post(
        "/admin/staff/create-account",
        data={**create_form(role=role), "csrf_token": csrf[1]},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 303, response.text
    target = UUID(response.headers["location"].rsplit("/", 1)[1])
    detail = browser.get(response.headers["location"])
    assert detail.status_code == 200 and "forms-new" in detail.text
    with staff_database.unit_of_work() as session:
        cli = create_staff(
            session,
            admin_login="forms-admin",
            admin_password=PASSWORD,
            login="forms-cli",
            password=NEW_PASSWORD,
            role=role,
        )
        assert cli.ok
        for uid in (target, cli.user_id):
            user = session.get(StaffUser, uid)
            assert user is not None and user.role == role and user.is_active
            assert user.password_hash.startswith("$argon2id$")
            assert verify_password(user.password_hash, NEW_PASSWORD)
            audit = session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "staff.create", AuditEvent.entity_id == uid
                )
            ).one()
            assert audit.result == "success"
            assert audit.actor_id == identifier(staff_database, "forms-admin")
            assert audit.details == {"role": role, "is_active": True}
    assert login(browser, "forms-new", NEW_PASSWORD).status_code == 200


@pytest.mark.parametrize("account", [None, "forms-manager"])
def test_direct_urls_are_admin_only(
    browser: TestClient, staff_database: Database, account: str | None
) -> None:
    csrf = login(browser, account).json()["csrf_token"] if account else "unused"
    uid = identifier(staff_database, "forms-admin")
    for method, path, data in (
        ("GET", "/admin/staff", {}),
        ("GET", f"/admin/staff/{uid}", {}),
        ("POST", "/admin/staff/create-account", create_form()),
        ("POST", f"/admin/staff/{uid}/disable", {"confirm": "disable"}),
    ):
        response = browser.request(method, path, data=data, headers=headers(csrf))
        assert response.status_code == (403 if account else 401), response.text
    if account:
        assert "Create staff account" not in browser.get("/admin/").text
    with staff_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(StaffUser)) == 2
        count = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "staff.access",
                AuditEvent.result
                == ("forbidden" if account else "authentication_denied"),
            )
        )
        assert count is not None and count >= 4


@pytest.mark.parametrize("action", ["create", "disable"])
@pytest.mark.parametrize("denial", ["csrf", "origin", "session"])
def test_mutation_security(
    browser: TestClient, staff_database: Database, action: str, denial: str
) -> None:
    csrf = login(browser).json()["csrf_token"]
    uid = identifier(staff_database, "forms-manager")
    url = (
        "/admin/staff/create-account"
        if action == "create"
        else f"/admin/staff/{uid}/disable"
    )
    data = create_form() if action == "create" else {"confirm": "disable"}
    request_headers = headers("forged" if denial == "csrf" else csrf)
    if denial == "origin":
        request_headers["Origin"] = "https://foreign.test"
    if denial == "session":
        browser.cookies.clear()
    response = browser.post(url, data=data, headers=request_headers)
    assert response.status_code == (401 if denial == "session" else 403)
    with staff_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(StaffUser)) == 2
        assert session.scalar(select(StaffUser.is_active).where(StaffUser.id == uid))
        action_name = "staff.access" if denial == "session" else "staff.csrf"
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == action_name)
            )
            == 1
        )


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"login": "bad login"}, 422),
        ({"login": "ab"}, 422),
        ({"login": "a" * 65}, 422),
        ({"login": "forms-manager"}, 409),
        ({"role": "owner"}, 422),
        ({"password": "short", "password_confirm": "short"}, 422),
        ({"password": "x" * 1025}, 422),
        ({"password_confirm": "different-confirmation"}, 422),
        ({"actor_id": "spoof"}, 422),
        ({"password_hash": "spoof"}, 422),
    ],
)
def test_invalid_create(
    browser: TestClient, staff_database: Database, changes: dict[str, str], status: int
) -> None:
    csrf = login(browser).json()["csrf_token"]
    response = browser.post(
        "/admin/staff/create-account",
        data=create_form(**changes),
        headers=headers(csrf),
    )
    assert response.status_code == status, response.text
    assert NEW_PASSWORD not in response.text
    with staff_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(StaffUser)) == 2
        assert session.scalars(
            select(AuditEvent.result).where(
                AuditEvent.action == "staff.create", AuditEvent.result != "success"
            )
        ).one() == ("login_exists" if status == 409 else "invalid_input")


@pytest.mark.parametrize("field", ["login", "role", "password", "csrf_token"])
def test_duplicate_create_fields(browser: TestClient, field: str) -> None:
    csrf = login(browser).json()["csrf_token"]
    data = {**create_form(), "csrf_token": csrf}
    pairs = [*data.items(), (field, data[field])]
    response = browser.post(
        "/admin/staff/create-account",
        content=urlencode(pairs),
        headers={**headers(csrf), "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("case", ["duplicate", "unknown", "query", "oversize"])
def test_strict_disable_form(
    browser: TestClient, staff_database: Database, case: str
) -> None:
    csrf = login(browser).json()["csrf_token"]
    uid = identifier(staff_database, "forms-manager")
    url = f"/admin/staff/{uid}/disable"
    pairs = [("confirm", "disable")]
    if case == "duplicate":
        pairs.append(("confirm", "disable"))
    elif case == "unknown":
        pairs.append(("is_active", "false"))
    elif case == "query":
        url += "?confirm=disable"
    else:
        pairs.append(("extra", "x" * 16384))
    response = browser.post(
        url,
        content=urlencode(pairs),
        headers={**headers(csrf), "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == (413 if case == "oversize" else 422)
    with staff_database.unit_of_work() as session:
        assert session.scalar(select(StaffUser.is_active).where(StaffUser.id == uid))


def test_revoked_admin_cannot_read_or_write(
    browser: TestClient, staff_database: Database
) -> None:
    csrf = login(browser).json()["csrf_token"]
    uid = identifier(staff_database, "forms-admin")
    with staff_database.unit_of_work() as session:
        row = session.scalars(
            select(StaffSession).where(StaffSession.user_id == uid)
        ).one()
        row.revoked_at = utc_now()
    for _ in range(2):
        assert browser.get("/admin/staff").status_code == 401
        assert browser.get(f"/admin/staff/{uid}").status_code == 401
        assert (
            browser.post(
                "/admin/staff/create-account", data=create_form(), headers=headers(csrf)
            ).status_code
            == 401
        )


def test_confirmation_and_no_get_mutations(
    browser: TestClient, staff_database: Database
) -> None:
    csrf = login(browser).json()["csrf_token"]
    uid = identifier(staff_database, "forms-manager")
    assert browser.get(f"/admin/staff/{uid}/disable").status_code == 405
    assert browser.get("/admin/staff/create-account").status_code >= 400
    assert (
        browser.post(
            f"/admin/staff/{uid}/disable", data={}, headers=headers(csrf)
        ).status_code
        == 422
    )
    assert (
        browser.post(
            f"/admin/staff/{uid}/disable",
            data={"confirm": "false"},
            headers=headers(csrf),
        ).status_code
        == 422
    )
    with staff_database.unit_of_work() as session:
        assert session.scalar(select(StaffUser.is_active).where(StaffUser.id == uid))


def test_disable_all_sessions_and_relogin(
    browser: TestClient, forms_app: FastAPI, staff_database: Database
) -> None:
    csrf = login(browser).json()["csrf_token"]
    uid = identifier(staff_database, "forms-manager")
    with (
        TestClient(forms_app, base_url=ORIGIN) as first,
        TestClient(forms_app, base_url=ORIGIN) as second,
    ):
        assert login(first, "forms-manager").status_code == 200
        assert login(second, "forms-manager").status_code == 200
        with staff_database.unit_of_work() as session:
            old = session.scalars(
                select(StaffSession).where(StaffSession.user_id == uid)
            ).first()
            assert old is not None
            old.revoked_at = utc_now() - timedelta(hours=1)
            old_id, old_revoked = old.id, old.revoked_at
        assert login(first, "forms-manager").status_code == 200
        assert (
            browser.post(
                f"/admin/staff/{uid}/disable",
                data={"confirm": "disable"},
                headers=headers(csrf),
            ).status_code
            == 303
        )
        for client in (first, second):
            for _ in range(2):
                assert client.get("/admin/staff").status_code == 401
                assert (
                    client.post(
                        "/admin/staff/create-account",
                        data=create_form(),
                        headers=headers(csrf),
                    ).status_code
                    == 401
                )
            assert login(client, "forms-manager").status_code == 401
    with staff_database.unit_of_work() as session:
        assert not session.scalar(
            select(StaffUser.is_active).where(StaffUser.id == uid)
        )
        sessions = session.scalars(
            select(StaffSession).where(StaffSession.user_id == uid)
        ).all()
        assert len(sessions) == 3 and all(row.revoked_at for row in sessions)
        assert session.get(StaffSession, old_id).revoked_at == old_revoked  # type: ignore[union-attr]
        audit = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "staff.disable")
        ).one()
        assert audit.result == "success" and audit.entity_id == uid
        assert audit.details == {"is_active": False}


def test_self_last_admin_disable_keeps_bootstrap_closed(
    browser: TestClient, staff_database: Database
) -> None:
    csrf = login(browser).json()["csrf_token"]
    cookie = browser.cookies[SESSION_COOKIE]
    uid = identifier(staff_database, "forms-admin")
    page = browser.get(f"/admin/staff/{uid}")
    assert "This is your account" in page.text
    assert "This is the last active administrator" in page.text
    response = browser.post(
        f"/admin/staff/{uid}/disable",
        data={"confirm": "disable"},
        headers=headers(csrf),
    )
    assert (
        response.status_code == 200 and "All your sessions have ended" in response.text
    )
    assert (
        "csrf_token" not in response.text
        and "Max-Age=0" in response.headers["set-cookie"]
    )
    browser.cookies.set(SESSION_COOKIE, cookie)
    assert browser.get("/admin/staff").status_code == 401
    assert login(browser).status_code == 401
    with staff_database.unit_of_work() as session:
        assert (
            bootstrap_admin(session, login="repeat-bootstrap", password=PASSWORD).result
            == "bootstrap_closed"
        )


def test_bounded_sorted_list_and_safe_context(
    browser: TestClient, staff_database: Database
) -> None:
    login(browser)
    with staff_database.unit_of_work() as session:
        password_hash = hash_password(PASSWORD)
        session.add_all(
            [
                StaffUser(
                    login=f"account-{n:03d}",
                    role="manager",
                    password_hash=password_hash,
                )
                for n in range(105)
            ]
        )
    response = browser.get("/admin/staff")
    assert response.status_code == 200 and "More accounts exist" in response.text
    assert response.text.count('<td><a href="/admin/staff/') == 100
    assert response.text.index("account-000") < response.text.index("account-099")
    assert "account-100" not in response.text
    for secret in (PASSWORD, password_hash, "password_hash", "token_hash"):
        assert secret not in response.text
    assert browser.get(f"/admin/staff/{uuid4()}").status_code == 404


@pytest.mark.parametrize("operation", ["create", "disable"])
def test_audit_failure_rolls_back_entire_command(
    browser: TestClient,
    staff_database: Database,
    operation: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    csrf = login(browser).json()["csrf_token"]
    uid = identifier(staff_database, "forms-manager")
    with staff_database.unit_of_work() as session:
        assert login_staff(session, "forms-manager", PASSWORD) is not None

    def fail(mapper: object, connection: Connection, target: AuditEvent) -> None:
        if target.action == f"staff.{operation}":
            connection.exec_driver_sql("SELECT 1 / 0")

    event.listen(AuditEvent, "before_insert", fail)
    try:
        url = (
            "/admin/staff/create-account"
            if operation == "create"
            else f"/admin/staff/{uid}/disable"
        )
        data = create_form() if operation == "create" else {"confirm": "disable"}
        response = browser.post(url, data=data, headers=headers(csrf))
        assert response.status_code == 503, response.text
    finally:
        event.remove(AuditEvent, "before_insert", fail)
    with staff_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(StaffUser)) == 2
        assert session.scalar(select(StaffUser.is_active).where(StaffUser.id == uid))
        assert (
            session.scalars(select(StaffSession).where(StaffSession.user_id == uid))
            .one()
            .revoked_at
            is None
        )
        assert session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == f"staff.{operation}")
        ) == (1 if operation == "create" else 0)
    for secret in (PASSWORD, NEW_PASSWORD, "SELECT 1 / 0", "password_hash"):
        assert secret not in response.text + caplog.text


def test_no_credentials_in_html_audit_or_logs(
    browser: TestClient,
    staff_database: Database,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    csrf = login(browser).json()["csrf_token"]
    raw = browser.cookies[SESSION_COOKIE]
    created = browser.post(
        "/admin/staff/create-account", data=create_form(), headers=headers(csrf)
    )
    assert created.status_code == 303
    detail = browser.get(created.headers["location"])
    with staff_database.unit_of_work() as session:
        user = session.scalars(
            select(StaffUser).where(StaffUser.login == "forms-new")
        ).one()
        password_hash = user.password_hash
        session_hashes = list(session.scalars(select(StaffSession.token_hash)))
        audit = json.dumps(
            [
                {"action": row.action, "result": row.result, "details": row.details}
                for row in session.scalars(select(AuditEvent))
            ]
        )
    captured = capsys.readouterr()
    visible = (
        created.text
        + str(dict(created.headers))
        + detail.text
        + audit
        + caplog.text
        + captured.out
        + captured.err
    )
    for secret in (PASSWORD, NEW_PASSWORD, raw, password_hash, *session_hashes):
        assert secret not in visible
    assert "forms-new" not in audit


def test_disable_wins_race_with_http_create(
    browser: TestClient, staff_database: Database
) -> None:
    csrf = login(browser).json()["csrf_token"]
    uid = identifier(staff_database, "forms-admin")
    locked, release, requested = Event(), Event(), Event()

    def disable() -> None:
        with staff_database.unit_of_work() as session:
            lock_staff(session)
            assert disable_staff(
                session, admin_login="forms-admin", admin_password=PASSWORD, user_id=uid
            ).ok
            locked.set()
            assert release.wait(5)

    def create() -> Response:
        requested.set()
        return browser.post(
            "/admin/staff/create-account", data=create_form(), headers=headers(csrf)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        disabling = pool.submit(disable)
        assert locked.wait(5)
        pending = pool.submit(create)
        assert requested.wait(5)
        release.set()
        disabling.result(timeout=10)
        assert pending.result(timeout=10).status_code == 401
    with staff_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(StaffUser)) == 2


@pytest.mark.parametrize("loss", ["disabled", "role", "revoked", "expired"])
def test_actor_command_refreshes_stale_authority(
    forms_app: FastAPI, staff_database: Database, loss: str
) -> None:
    with staff_database.unit_of_work() as session:
        issued = login_staff(session, "forms-admin", PASSWORD)
        assert issued is not None
        raw = issued.token
    with staff_database.sessions() as session:
        lock_staff(session)
        actor = resolve_session(session, raw)
        assert actor is not None
        session.commit()
        with staff_database.unit_of_work() as another:
            lock_staff(another)
            if loss in {"disabled", "role"}:
                user = another.get(StaffUser, actor.user.id)
                assert user is not None
                if loss == "disabled":
                    user.is_active = False
                else:
                    user.role = "manager"
            else:
                row = another.get(StaffSession, actor.session.id)
                assert row is not None
                if loss == "revoked":
                    row.revoked_at = utc_now()
                else:
                    row.expires_at = utc_now() - timedelta(seconds=1)
        outcome = create_staff_as_actor(
            session, actor=actor, login="stale-created", password=PASSWORD, role="admin"
        )
        assert outcome.result == (
            "forbidden" if loss == "role" else "authentication_denied"
        )
        session.commit()


def test_actor_rejects_detached_identity(
    forms_app: FastAPI, staff_database: Database
) -> None:
    with staff_database.unit_of_work() as session:
        actor = authenticate_operator(session, "forms-admin", PASSWORD)
        assert actor is not None
    with staff_database.unit_of_work() as session:
        outcome = create_staff_as_actor(
            session,
            actor=actor,
            login="detached-created",
            password=PASSWORD,
            role="admin",
        )
        assert outcome.result == "authentication_denied"


def test_actor_cli_disable_same_postconditions(
    forms_app: FastAPI, staff_database: Database
) -> None:
    with staff_database.unit_of_work() as session:
        actor = authenticate_operator(session, "forms-admin", PASSWORD)
        assert actor is not None
        created = create_staff_as_actor(
            session, actor=actor, login="actor-admin", password=PASSWORD, role="admin"
        )
        assert created.ok and created.user_id is not None
        issued = login_staff(session, "actor-admin", PASSWORD)
        assert issued is not None
        assert disable_staff_as_actor(session, actor=actor, user_id=created.user_id).ok
        session.refresh(issued.identity.session)
        assert not issued.identity.user.is_active and issued.identity.session.revoked_at
        assert disable_staff(
            session,
            admin_login="forms-admin",
            admin_password=PASSWORD,
            user_id=actor.user.id,
        ).ok
        assert (
            bootstrap_admin(session, login="closed-bootstrap", password=PASSWORD).result
            == "bootstrap_closed"
        )
