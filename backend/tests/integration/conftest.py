"""Destructive fixtures are restricted to the dedicated test container stack."""

import os
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.db import Database, DatabaseSettings
from app.core.db_types import utc_now
from app.core.staff import bootstrap_admin, create_staff
from app.core.staff_models import StaffSession


@pytest.fixture(scope="session")
def databases() -> Iterator[tuple[Database, Database]]:
    if (
        os.environ.get("DB_INTEGRATION_ISOLATED") != "1"
        or os.environ.get("DB_HOST") != "postgres-test"
        or os.environ.get("DB_NAME", "akbrizcase") != "akbrizcase"
    ):
        pytest.fail(
            "Use the isolated compose.integration.yml stack for integration tests"
        )
    runtime = Database(DatabaseSettings.from_environment())
    migrator = Database(DatabaseSettings.from_environment(migration=True))
    try:
        with migrator.engine.connect() as connection:
            assert str(connection.scalar(text("SHOW server_version"))).startswith("16.")
            # Refuse any existing tables BEFORE migrations or test cleanup.
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM pg_tables WHERE schemaname = 'app'")
                )
                == 0
            )
        assert not runtime.ready()
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        command.upgrade(config, "0001_infrastructure")
        assert not runtime.ready()
        with migrator.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE app.integration_probe ("
                    "id uuid PRIMARY KEY, amount numeric(12,2) NOT NULL, "
                    "occurred_at timestamptz NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO app.integration_probe VALUES "
                    "('00000000-0000-4000-8000-000000000001', 123.45, now())"
                )
            )
        # Simulate an existing MVP-004 installation, with real Argon2id hashes,
        # two staff identities, a server session and their append-only audit.
        command.upgrade(config, "0002_mvp004_staff")
        assert not runtime.ready()
        with runtime.unit_of_work() as session:
            actor = bootstrap_admin(
                session,
                login="synthetic-upgrade-admin",
                password="synthetic-upgrade-admin-password",
            )
            assert actor.ok and actor.user_id is not None
            manager = create_staff(
                session,
                admin_login="synthetic-upgrade-admin",
                admin_password="synthetic-upgrade-admin-password",
                login="synthetic-upgrade-manager",
                password="synthetic-upgrade-manager-password",
                role="manager",
            )
            assert manager.ok
            session.add(
                StaffSession(
                    user_id=actor.user_id,
                    token_hash="a" * 64,
                    expires_at=utc_now() + timedelta(hours=8),
                )
            )
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM app.alembic_version"))
                == "0002_mvp004_staff"
            )
            assert (
                connection.scalar(text("SELECT to_regclass('app.catalog_items')"))
                is None
            )
            preserved_staff = {
                table: connection.execute(
                    text(f"SELECT * FROM app.{table} ORDER BY id")
                ).all()
                for table in ("staff_users", "staff_sessions", "audit_events")
            }
            assert len(preserved_staff["staff_users"]) == 2
            assert len(preserved_staff["staff_sessions"]) == 1
            assert len(preserved_staff["audit_events"]) == 2
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        with migrator.engine.begin() as connection:
            for table, rows in preserved_staff.items():
                assert (
                    connection.execute(
                        text(f"SELECT * FROM app.{table} ORDER BY id")
                    ).all()
                    == rows
                )
            assert connection.scalar(
                text("SELECT amount FROM app.integration_probe")
            ) == Decimal("123.45")
            connection.execute(text("DELETE FROM app.integration_probe"))
            # Only this sentinel-guarded disposable stack's migrator cleans up
            # the synthetic setup; production/runtime cannot erase audit/users.
            connection.execute(text("DELETE FROM app.audit_events"))
            connection.execute(text("DELETE FROM app.staff_sessions"))
            connection.execute(text("DELETE FROM app.staff_users"))
        yield runtime, migrator
    finally:
        runtime.close()
        migrator.close()


@pytest.fixture
def database(databases: tuple[Database, Database]) -> Iterator[Database]:
    runtime, migrator = databases
    try:
        yield runtime
    finally:
        with migrator.engine.begin() as connection:
            connection.execute(text("DELETE FROM app.integration_probe"))


@pytest.fixture
def staff_database(databases: tuple[Database, Database]) -> Iterator[Database]:
    runtime, migrator = databases
    try:
        yield runtime
    finally:
        # Only the sentinel-guarded disposable DB's migrator can remove audit.
        with migrator.engine.begin() as connection:
            connection.execute(text("DELETE FROM app.audit_events"))
            connection.execute(text("DELETE FROM app.staff_sessions"))
            connection.execute(text("DELETE FROM app.staff_users"))
