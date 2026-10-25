"""Destructive fixtures are restricted to the dedicated test container stack."""

import os
from collections.abc import Iterator
from decimal import Decimal
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
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        with migrator.engine.begin() as connection:
            assert connection.scalar(
                text("SELECT amount FROM app.integration_probe")
            ) == Decimal("123.45")
            connection.execute(text("DELETE FROM app.integration_probe"))
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
