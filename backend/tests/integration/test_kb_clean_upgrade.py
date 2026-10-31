"""One-shot full upgrade on a second, separately labelled empty PostgreSQL 16."""

import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.db import SCHEMA_REVISION, Database, DatabaseSettings
from app.core.db_types import Base, utc_now
from app.modules.orders.deletion_journal import (
    DeletionCommand,
    SyntheticJournal,
    command_for,
    verify_witness,
)
from app.modules.orders.deletion_models import DeletionEntry
from app.modules.orders.seed import DEMO_VERSION, seed_consent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("DB_CLEAN_UPGRADE_ONLY") != "1",
        reason="Run by the isolated clean-upgrade phase of make integration",
    ),
]


def test_empty_postgresql_full_upgrade(tmp_path: Path) -> None:
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
        command.upgrade(config, "0009_mvp038_cleanup")
        seed_consent(runtime, mode="test")
        at = utc_now()
        legacy = DeletionCommand(
            id=uuid4(),
            operation="order_delete",
            object_id=uuid4(),
            consent_id=uuid4(),
            decision_id=uuid4(),
            policy_version=DEMO_VERSION,
            created_at=at,
            retain_until=at + timedelta(days=14),
        )
        with migrator.unit_of_work() as session:
            # Use predecessor columns, including a genuinely witnessed v1 shape.
            session.execute(
                text(
                    "INSERT INTO app.deletion_journal(id,operation,object_id,"
                    "consent_id,"
                    "decision_id,policy_version,created_at,retain_until) VALUES "
                    "(:id,:operation,:object_id,:consent_id,:decision_id,:policy_version,"
                    ":created_at,:retain_until)"
                ),
                {
                    key: getattr(legacy, key)
                    for key in (
                        "id",
                        "operation",
                        "object_id",
                        "consent_id",
                        "decision_id",
                        "policy_version",
                        "created_at",
                        "retain_until",
                    )
                },
            )
        tmp_path.chmod(0o700)
        sink = SyntheticJournal(tmp_path)
        receipt = sink.export(legacy, synthetic=True)
        preserved = (tmp_path / (str(legacy.id) + ".json")).read_bytes()
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert runtime.ready()
        with migrator.unit_of_work() as session:
            entry = session.get(DeletionEntry, legacy.id)
            assert (
                entry is not None
                and entry.format == 1
                and entry.journal_policy_version is None
            )
            assert command_for(entry).canonical() == legacy.canonical()
            assert (
                verify_witness(
                    sink, command_for(entry), receipt, synthetic=True
                ).canonical
                == preserved
            )
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
