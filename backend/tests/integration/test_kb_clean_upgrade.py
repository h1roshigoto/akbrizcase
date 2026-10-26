"""One-shot full upgrade on a second, separately labelled empty PostgreSQL 16."""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.db import SCHEMA_REVISION, Database, DatabaseSettings
from app.core.db_types import Base

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("DB_CLEAN_UPGRADE_ONLY") != "1",
        reason="Run by the isolated clean-upgrade phase of make integration",
    ),
]


def test_empty_postgresql_full_upgrade() -> None:
    assert os.environ.get("DB_INTEGRATION_ISOLATED") == "1"
    assert os.environ.get("DB_HOST") == "postgres-test"
    assert os.environ.get("DB_NAME", "akbrizcase") == "akbrizcase"
    runtime = Database(DatabaseSettings.from_environment())
    migrator = Database(DatabaseSettings.from_environment(migration=True))
    try:
        with migrator.engine.connect() as connection:
            assert str(connection.scalar(text("SHOW server_version"))).startswith("16.")
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM pg_tables WHERE schemaname='app'")
                )
                == 0
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT shobj_description(oid,'pg_database') FROM pg_database "
                        "WHERE datname=current_database()"
                    )
                )
                == "akbrizcase:synthetic-seed:test"
            )
        assert not runtime.ready()
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert runtime.ready()
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM app.alembic_version"))
                == SCHEMA_REVISION
            )
            assert set(
                connection.scalars(
                    text(
                        "SELECT 'app.' || tablename FROM pg_tables "
                        "WHERE schemaname='app' AND tablename <> 'alembic_version'"
                    )
                )
            ) == set(Base.metadata.tables)
    finally:
        runtime.close()
        migrator.close()
