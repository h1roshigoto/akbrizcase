"""Destructive fixtures are restricted to the dedicated test container stack."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.db import Database, DatabaseSettings


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
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        with migrator.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE app.integration_probe ("
                    "id uuid PRIMARY KEY, amount numeric(12,2) NOT NULL, "
                    "occurred_at timestamptz NOT NULL)"
                )
            )
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
