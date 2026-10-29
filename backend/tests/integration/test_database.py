"""Real transactions, migrations, permissions and HTTP readiness on PostgreSQL 16."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.db import SCHEMA_REVISION, Database
from app.main import app

pytestmark = pytest.mark.integration


def insert_probe(session: Session, identifier: UUID) -> None:
    # Future services share the caller's Session. No inner commits.
    session.execute(
        text("INSERT INTO app.integration_probe VALUES (:id, :amount, :occurred_at)"),
        {
            "id": identifier,
            "amount": Decimal("123.45"),
            "occurred_at": datetime.now(UTC),
        },
    )


def count_rows(database: Database) -> int:
    with database.engine.connect() as connection:
        return int(
            connection.scalar(text("SELECT count(*) FROM app.integration_probe"))
        )


def test_migration_from_staff_and_repeat(databases: tuple[Database, Database]) -> None:
    runtime, migrator = databases
    assert runtime.ready()
    assert not migrator.ready()  # Schema-owner credentials are rejected by readiness.
    with runtime.engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM app.alembic_version")
        ) == (SCHEMA_REVISION)
        assert connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm'")
        )
        assert connection.scalar(text("SHOW timezone")) == "UTC"
        assert set(
            connection.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'app'")
            )
        ) == {
            "alembic_version",
            "integration_probe",
            "equipment_models",
            "catalog_items",
            "catalog_item_models",
            "documents",
            "document_versions",
            "document_sections",
            "staff_users",
            "consent_texts",
            "consents",
            "orders",
            "order_items",
            "order_status_history",
            "email_outbox",
            "staff_sessions",
            "audit_events",
            "diag_trees",
            "diag_tree_sources",
            "diag_tree_items",
            "chat_sessions",
            "chat_messages",
        }


def test_two_operations_commit_together(database: Database) -> None:
    with database.unit_of_work() as session:
        insert_probe(session, uuid4())
        insert_probe(session, uuid4())
        # A separate connection cannot observe uncommitted writes.
        assert count_rows(database) == 0
    assert count_rows(database) == 2
    with database.engine.connect() as connection:
        row = connection.execute(
            text("SELECT * FROM app.integration_probe LIMIT 1")
        ).one()
        assert isinstance(row.id, UUID) and row.id.version == 4
        assert row.amount == Decimal("123.45") and isinstance(row.amount, Decimal)
        assert row.occurred_at.utcoffset().total_seconds() == 0
    assert database.engine.pool.checkedout() == 0  # type: ignore[attr-defined]


def test_exception_rolls_back_both_operations(database: Database) -> None:
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with database.unit_of_work() as session:
            insert_probe(session, uuid4())
            insert_probe(session, uuid4())
            raise RuntimeError("synthetic failure")
    assert count_rows(database) == 0
    # Session/connection can be reused after rollback.
    with database.unit_of_work() as session:
        insert_probe(session, uuid4())
    assert count_rows(database) == 1


def test_sql_error_rolls_back_and_releases_connection(database: Database) -> None:
    identifier = uuid4()
    with pytest.raises(DBAPIError):
        with database.unit_of_work() as session:
            insert_probe(session, identifier)
            insert_probe(session, identifier)
    assert count_rows(database) == 0
    assert database.ready()


def test_runtime_permissions(database: Database) -> None:
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == "akbrizcase_runtime"
        assert (
            connection.scalar(
                text(
                    "SELECT NOT (rolsuper OR rolcreatedb OR rolcreaterole "
                    "OR rolbypassrls "
                    "OR rolreplication) FROM pg_roles WHERE rolname = current_user"
                )
            )
            is True
        )
    for query in (
        "CREATE TABLE app.forbidden (id integer)",
        "CREATE TABLE public.forbidden (id integer)",
        "CREATE SCHEMA forbidden",
        "CREATE ROLE forbidden",
        "UPDATE app.alembic_version SET version_num = 'tampered'",
        "SET ROLE akbrizcase_migrator",
        "SET ROLE akbrizcase_owner",
    ):
        with pytest.raises(DBAPIError):
            with database.unit_of_work() as session:
                session.execute(text(query))
    with database.unit_of_work() as session:
        identifier = uuid4()
        insert_probe(session, identifier)
        session.execute(text("UPDATE app.integration_probe SET amount = 1"))
        session.execute(
            text("DELETE FROM app.integration_probe WHERE id = :id"), {"id": identifier}
        )


def test_ready_and_live(database: Database) -> None:
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert response.headers["cache-control"] == "no-store"


def test_unreachable_database(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_PORT", "1")
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}


@pytest.mark.parametrize(
    "revision",
    [
        "0001_infrastructure",
        "0002_mvp004_staff",
        "0002_mvp006_catalog",
        "unknown_future_schema",
        None,
    ],
)
def test_incompatible_or_missing_revision(
    databases: tuple[Database, Database], revision: str | None
) -> None:
    runtime, migrator = databases
    with migrator.engine.begin() as connection:
        connection.execute(text("DELETE FROM app.alembic_version"))
        if revision is not None:
            connection.execute(
                text("INSERT INTO app.alembic_version VALUES (:version)"),
                {"version": revision},
            )
    try:
        assert not runtime.ready()
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            response = client.get("/readyz")
            assert response.status_code == 503
            assert response.json() == {"status": "unavailable"}
    finally:
        with migrator.engine.begin() as connection:
            connection.execute(text("DELETE FROM app.alembic_version"))
            connection.execute(
                text("INSERT INTO app.alembic_version VALUES (:version)"),
                {"version": SCHEMA_REVISION},
            )
    assert runtime.ready()


def test_single_linear_migration_history() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [SCHEMA_REVISION]
    assert [revision.revision for revision in scripts.walk_revisions()] == [
        "0008_mvp037_privacy",
        "0007_mvp029_chat",
        "0006_mvp027_diagnostic_trees",
        "0005_mvp024_applicability",
        "0004_mvp019_documents",
        "0003_mvp008_orders",
        "0002_mvp006_catalog",
        "0002_mvp004_staff",
        "0001_infrastructure",
    ]
