"""Real PostgreSQL staff security, concurrency and transactional audit checks."""

import hashlib
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from app.core import cli
from app.core.auth import StaffRole, authenticate, has_role, verify_password
from app.core.db import Database
from app.core.db_types import Base, utc_now
from app.core.staff import StaffOutcome, bootstrap_admin, create_staff, disable_staff
from app.core.staff_models import AuditEvent, StaffSession, StaffUser

pytestmark = pytest.mark.integration
ADMIN_LOGIN = "synthetic-admin"
ADMIN_PASSWORD = "synthetic-admin-individual-password"
STAFF_PASSWORD = "synthetic-staff-individual-password"


def bootstrap(database: Database) -> UUID:
    with database.unit_of_work() as session:
        outcome = bootstrap_admin(session, login=ADMIN_LOGIN, password=ADMIN_PASSWORD)
        assert outcome.ok and outcome.user_id is not None
        return outcome.user_id


def create(database: Database, role: StaffRole = "manager") -> UUID:
    with database.unit_of_work() as session:
        outcome = create_staff(
            session,
            admin_login=ADMIN_LOGIN,
            admin_password=ADMIN_PASSWORD,
            login="synthetic-new-" + role,
            password=STAFF_PASSWORD,
            role=role,
        )
        assert outcome.ok and outcome.user_id is not None
        return outcome.user_id


def test_empty_bootstrap_and_repeat(staff_database: Database) -> None:
    database = staff_database
    identifier = bootstrap(database)
    with database.unit_of_work() as session:
        user = session.get(StaffUser, identifier)
        assert user is not None and user.is_active and user.role == "admin"
        assert verify_password(user.password_hash, ADMIN_PASSWORD)
        assert authenticate(session, ADMIN_LOGIN, ADMIN_PASSWORD).id == identifier  # type: ignore[union-attr]
        audit = session.scalars(select(AuditEvent)).one()
        assert audit.actor_id == audit.entity_id == identifier
        assert audit.action == "staff.bootstrap" and audit.result == "success"
        assert audit.details == {"role": "admin", "is_active": True}
        assert audit.at.utcoffset() == timedelta(0)
        outcome = bootstrap_admin(
            session, login="another-admin", password=STAFF_PASSWORD
        )
        assert outcome.result == "bootstrap_closed"
    with database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(StaffUser)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 2


def test_concurrent_bootstrap(staff_database: Database) -> None:
    barrier = Barrier(2)

    def attempt(number: int) -> StaffOutcome:
        with staff_database.unit_of_work() as session:
            barrier.wait(timeout=5)
            return bootstrap_admin(
                session,
                login=f"synthetic-admin-{number}",
                password=ADMIN_PASSWORD,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sorted(result.result for result in results) == [
        "bootstrap_closed",
        "success",
    ]
    with staff_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(StaffUser)) == 1
        assert sorted(session.scalars(select(AuditEvent.result))) == [
            "bootstrap_closed",
            "success",
        ]


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_active_admin_creates_staff(staff_database: Database, role: StaffRole) -> None:
    actor_id = bootstrap(staff_database)
    target_id = create(staff_database, role)
    with staff_database.unit_of_work() as session:
        user = authenticate(session, "synthetic-new-" + role, STAFF_PASSWORD)
        assert user is not None and user.id == target_id and user.role == role
        assert has_role(user, "manager")
        audit = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "staff.create")
        ).one()
        assert audit.actor_id == actor_id and audit.entity_id == target_id
        assert audit.result == "success" and audit.details["role"] == role


@pytest.mark.parametrize("kind", ["wrong_password", "unknown", "inactive", "manager"])
@pytest.mark.parametrize("operation", ["create", "disable"])
def test_unauthorized_operations_are_denied_and_audited(
    staff_database: Database,
    kind: str,
    operation: str,
) -> None:
    database = staff_database
    actor_id = bootstrap(database)
    target_id = create(database)
    login, password = ADMIN_LOGIN, ADMIN_PASSWORD
    if kind == "wrong_password":
        password = "synthetic-wrong-password"
    elif kind == "unknown":
        login = "synthetic-unknown"
    elif kind == "manager":
        login, password = "synthetic-new-manager", STAFF_PASSWORD
    else:
        with database.unit_of_work() as session:
            assert disable_staff(
                session,
                admin_login=login,
                admin_password=password,
                user_id=actor_id,
            ).ok
    with database.unit_of_work() as session:
        if operation == "create":
            outcome = create_staff(
                session,
                admin_login=login,
                admin_password=password,
                login="forbidden-admin",
                password=STAFF_PASSWORD,
                role="admin",
            )
        else:
            outcome = disable_staff(
                session,
                admin_login=login,
                admin_password=password,
                user_id=target_id,
            )
        expected = "forbidden" if kind == "manager" else "authentication_denied"
        assert outcome.result == expected
    with database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(StaffUser)) == 2
        target = session.get(StaffUser, target_id)
        assert target is not None and target.is_active
        audit = session.scalars(
            select(AuditEvent).where(AuditEvent.result == expected)
        ).one()
        assert audit.actor_id == (target_id if kind == "manager" else None)
        assert audit.action == "staff." + operation and audit.details == {}


def add_sessions(database: Database, user_id: UUID) -> UUID:
    with database.unit_of_work() as session:
        live = StaffSession(
            user_id=user_id,
            token_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            expires_at=utc_now() + timedelta(hours=8),
        )
        revoked = StaffSession(
            user_id=user_id,
            token_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            expires_at=utc_now() + timedelta(hours=8),
            revoked_at=utc_now() - timedelta(hours=1),
        )
        session.add_all([live, revoked])
        session.flush()
        return revoked.id


def test_disable_revokes_sessions_and_closes_bootstrap(
    staff_database: Database,
) -> None:
    actor_id = bootstrap(staff_database)
    target_id = create(staff_database)
    revoked_id = add_sessions(staff_database, target_id)
    with staff_database.unit_of_work() as session:
        previous = session.get(StaffSession, revoked_id)
        assert previous is not None
        revoked_at = previous.revoked_at
        assert disable_staff(
            session,
            admin_login=ADMIN_LOGIN,
            admin_password=ADMIN_PASSWORD,
            user_id=target_id,
        ).ok
    with staff_database.unit_of_work() as session:
        sessions = list(session.scalars(select(StaffSession)))
        assert all(item.revoked_at is not None for item in sessions)
        assert session.get(StaffSession, revoked_id).revoked_at == revoked_at  # type: ignore[union-attr]
        assert authenticate(session, "synthetic-new-manager", STAFF_PASSWORD) is None
        audit = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "staff.disable")
        ).one()
        assert audit.actor_id == actor_id and audit.entity_id == target_id
        assert audit.details == {"is_active": False}
        assert disable_staff(
            session,
            admin_login=ADMIN_LOGIN,
            admin_password=ADMIN_PASSWORD,
            user_id=actor_id,
        ).ok
    with staff_database.unit_of_work() as session:
        assert (
            bootstrap_admin(session, login="new-admin", password=STAFF_PASSWORD).result
            == "bootstrap_closed"
        )


@pytest.mark.parametrize("action", ["bootstrap", "create", "disable"])
def test_audit_failure_rolls_back_action_and_sessions(
    staff_database: Database,
    action: str,
) -> None:
    target_id = None
    if action != "bootstrap":
        bootstrap(staff_database)
    if action == "disable":
        target_id = create(staff_database)
        add_sessions(staff_database, target_id)

    def fail_audit(*args: object) -> None:
        raise RuntimeError("synthetic audit storage failure")

    event.listen(AuditEvent, "before_insert", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="synthetic audit storage failure"):
            with staff_database.unit_of_work() as session:
                if action == "bootstrap":
                    bootstrap_admin(session, login=ADMIN_LOGIN, password=ADMIN_PASSWORD)
                elif action == "create":
                    create_staff(
                        session,
                        admin_login=ADMIN_LOGIN,
                        admin_password=ADMIN_PASSWORD,
                        login="rollback-user",
                        password=STAFF_PASSWORD,
                        role="manager",
                    )
                else:
                    assert target_id is not None
                    disable_staff(
                        session,
                        admin_login=ADMIN_LOGIN,
                        admin_password=ADMIN_PASSWORD,
                        user_id=target_id,
                    )
    finally:
        event.remove(AuditEvent, "before_insert", fail_audit)
    with staff_database.unit_of_work() as session:
        expected = {"bootstrap": 0, "create": 1, "disable": 2}[action]
        assert session.scalar(select(func.count()).select_from(StaffUser)) == expected
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == expected
        if action == "disable":
            assert session.get(StaffUser, target_id).is_active  # type: ignore[union-attr]
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(StaffSession)
                    .where(StaffSession.revoked_at.is_(None))
                )
                == 1
            )
    if action == "bootstrap":
        bootstrap(staff_database)  # a rolled-back attempt does not consume bootstrap


def test_outer_failure_rolls_back_audit_with_action(staff_database: Database) -> None:
    bootstrap(staff_database)
    with pytest.raises(RuntimeError):
        with staff_database.unit_of_work() as session:
            assert create_staff(
                session,
                admin_login=ADMIN_LOGIN,
                admin_password=ADMIN_PASSWORD,
                login="rollback-user",
                password=STAFF_PASSWORD,
                role="manager",
            ).ok
            session.flush()
            raise RuntimeError("synthetic later service failure")
    with staff_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(StaffUser)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_disable_wins_before_waiting_admin_operation(staff_database: Database) -> None:
    actor_id = bootstrap(staff_database)
    waiting = Event()

    def create_after_disable() -> StaffOutcome:
        with staff_database.unit_of_work() as session:
            waiting.set()
            return create_staff(
                session,
                admin_login=ADMIN_LOGIN,
                admin_password=ADMIN_PASSWORD,
                login="racing-user",
                password=STAFF_PASSWORD,
                role="admin",
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with staff_database.unit_of_work() as session:
            assert disable_staff(
                session,
                admin_login=ADMIN_LOGIN,
                admin_password=ADMIN_PASSWORD,
                user_id=actor_id,
            ).ok
            future = pool.submit(create_after_disable)
            assert waiting.wait(timeout=2)
        assert future.result(timeout=5).result == "authentication_denied"


def test_cli_lifecycle_and_secret_minimization(
    staff_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")

    def run(argv: list[str], values: list[str]) -> int:
        answers = iter(values)
        monkeypatch.setattr(cli, "getpass", lambda prompt: next(answers))
        return cli.main(argv)

    assert run(["staff-bootstrap"], [ADMIN_LOGIN, ADMIN_PASSWORD, ADMIN_PASSWORD]) == 0
    assert (
        run(
            ["staff-create", "--role", "manager"],
            [
                ADMIN_LOGIN,
                ADMIN_PASSWORD,
                "synthetic-new-manager",
                STAFF_PASSWORD,
                STAFF_PASSWORD,
            ],
        )
        == 0
    )
    with staff_database.unit_of_work() as session:
        identifier = session.scalars(
            select(StaffUser.id).where(StaffUser.role == "manager")
        ).one()
    assert (
        run(
            ["staff-disable", "--user-id", str(identifier)],
            [ADMIN_LOGIN, "synthetic-wrong-password"],
        )
        == 1
    )
    assert (
        run(
            ["staff-disable", "--user-id", str(identifier)],
            [ADMIN_LOGIN, ADMIN_PASSWORD],
        )
        == 0
    )
    with staff_database.unit_of_work() as session:
        audit_json = json.dumps(
            [
                dict(row._mapping)
                for row in session.execute(text("SELECT * FROM app.audit_events"))
            ],
            default=str,
        )
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 4
        hashes = list(session.scalars(select(StaffUser.password_hash)))
    captured = capsys.readouterr()
    output = audit_json + captured.out + captured.err + caplog.text
    for secret in (
        ADMIN_PASSWORD,
        STAFF_PASSWORD,
        "synthetic-wrong-password",
        ADMIN_LOGIN,
        "synthetic-new-manager",
        *hashes,
    ):
        assert secret not in output
    assert '"authentication_denied"' in audit_json


def test_constraints_grants_and_model_migration_match(
    staff_database: Database,
    databases: tuple[Database, Database],
) -> None:
    identifier = bootstrap(staff_database)
    for query in (
        "UPDATE app.audit_events SET result = 'tampered'",
        "DELETE FROM app.audit_events",
        "TRUNCATE app.audit_events",
        "DELETE FROM app.staff_users",
        "TRUNCATE app.staff_users CASCADE",
    ):
        with pytest.raises(DBAPIError):
            with staff_database.unit_of_work() as session:
                session.execute(text(query))
    for change in (
        "role = 'owner'",
        "password_hash = 'plaintext'",
        "login = 'bad contact@example.org'",
    ):
        with pytest.raises(DBAPIError):
            with staff_database.unit_of_work() as session:
                session.execute(text("UPDATE app.staff_users SET " + change))
    with pytest.raises(DBAPIError):
        with staff_database.unit_of_work() as session:
            session.add(
                StaffSession(user_id=uuid4(), token_hash="a" * 64, expires_at=utc_now())
            )
    add_sessions(staff_database, identifier)
    with pytest.raises(DBAPIError):
        with staff_database.unit_of_work() as session:
            session.add(
                StaffSession(
                    user_id=identifier,
                    token_hash="plaintext-token",
                    expires_at=utc_now(),
                )
            )
    _, migrator = databases
    with migrator.engine.connect() as connection:
        # Explicit schemas must be reflected outside the application's search_path.
        connection.execute(text("SET LOCAL search_path TO public"))
        previous_schema = connection.dialect.default_schema_name
        connection.dialect.default_schema_name = "public"
        try:
            context = MigrationContext.configure(
                connection,
                opts={
                    "include_schemas": True,
                    "include_object": lambda obj, name, kind, reflected, compare_to: (
                        name not in {"integration_probe", "alembic_version"}
                    ),
                },
            )
            assert compare_metadata(context, Base.metadata) == []
        finally:
            connection.dialect.default_schema_name = previous_schema


def test_identity_map_cannot_reuse_disabled_admin(staff_database: Database) -> None:
    actor_id = bootstrap(staff_database)
    with staff_database.unit_of_work() as waiting_session:
        cached = waiting_session.get(StaffUser, actor_id)
        assert cached is not None and cached.is_active
        with staff_database.unit_of_work() as disabling_session:
            assert disable_staff(
                disabling_session,
                admin_login=ADMIN_LOGIN,
                admin_password=ADMIN_PASSWORD,
                user_id=actor_id,
            ).ok
        outcome = create_staff(
            waiting_session,
            admin_login=ADMIN_LOGIN,
            admin_password=ADMIN_PASSWORD,
            login="stale-identity-user",
            password=STAFF_PASSWORD,
            role="admin",
        )
        assert outcome.result == "authentication_denied"


def test_bootstrap_marker_survives_role_change(staff_database: Database) -> None:
    bootstrap(staff_database)
    with staff_database.unit_of_work() as session:
        # Simulate a later administrative data repair; marker remains authoritative.
        session.execute(text("UPDATE app.staff_users SET role = 'manager'"))
    with staff_database.unit_of_work() as session:
        assert (
            bootstrap_admin(
                session,
                login="replacement-admin",
                password=STAFF_PASSWORD,
            ).result
            == "bootstrap_closed"
        )


def test_duplicate_login_and_session_hash_rejected(staff_database: Database) -> None:
    actor_id = bootstrap(staff_database)
    with staff_database.unit_of_work() as session:
        outcome = create_staff(
            session,
            admin_login=ADMIN_LOGIN,
            admin_password=ADMIN_PASSWORD,
            login=ADMIN_LOGIN,
            password=STAFF_PASSWORD,
            role="manager",
        )
        assert outcome.result == "login_exists"
    with pytest.raises(DBAPIError):
        with staff_database.unit_of_work() as session:
            session.add(
                StaffUser(
                    login=ADMIN_LOGIN,
                    password_hash="$argon2id$synthetic",
                    role="manager",
                )
            )
    with pytest.raises(DBAPIError):
        with staff_database.unit_of_work() as session:
            session.add_all(
                [
                    StaffSession(
                        user_id=actor_id, token_hash="f" * 64, expires_at=utc_now()
                    ),
                    StaffSession(
                        user_id=actor_id, token_hash="f" * 64, expires_at=utc_now()
                    ),
                ]
            )


def test_staff_references_restrict_deletion(
    staff_database: Database,
    databases: tuple[Database, Database],
) -> None:
    actor_id = bootstrap(staff_database)
    _, migrator = databases
    # Use the schema owner solely to reach the FK; runtime DELETE is also denied.
    with pytest.raises(DBAPIError):
        with migrator.unit_of_work() as session:
            session.execute(
                text("DELETE FROM app.staff_users WHERE id = :id"), {"id": actor_id}
            )
    add_sessions(staff_database, actor_id)
    with pytest.raises(DBAPIError):
        with migrator.unit_of_work() as session:
            session.execute(text("DELETE FROM app.audit_events"))
            session.execute(
                text("DELETE FROM app.staff_users WHERE id = :id"), {"id": actor_id}
            )


def test_no_unauthenticated_creation_before_bootstrap(staff_database: Database) -> None:
    with staff_database.unit_of_work() as session:
        assert (
            create_staff(
                session,
                admin_login=ADMIN_LOGIN,
                admin_password=ADMIN_PASSWORD,
                login="unauthorized-first-admin",
                password=STAFF_PASSWORD,
                role="admin",
            ).result
            == "authentication_denied"
        )
        assert session.scalar(select(func.count()).select_from(StaffUser)) == 0
    bootstrap(staff_database)
