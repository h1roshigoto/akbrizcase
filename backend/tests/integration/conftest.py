"""Destructive fixtures are restricted to the dedicated test container stack."""

import os
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.db import Database, DatabaseSettings
from app.core.db_types import utc_now
from app.core.staff import bootstrap_admin, create_staff
from app.core.staff_models import StaffSession
from app.modules.catalog.seed import seed_catalog
from app.modules.notify.models import EmailOutbox
from app.modules.orders.models import Consent, ConsentText, Order, OrderItem


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
        # Existing MVP-007/MVP-012 data, including archive state and revisions,
        # must survive the new schema without table recreation or reseeding.
        command.upgrade(config, "0002_mvp006_catalog")
        assert not runtime.ready()
        seed_catalog(runtime, mode="test")
        with runtime.unit_of_work() as session:
            session.execute(
                text(
                    "UPDATE app.catalog_items SET revision = 7, status = 'archived' "
                    "WHERE sku = 'DEMO-P-001'"
                )
            )
            session.execute(text("UPDATE app.equipment_models SET status = 'archived'"))
        with runtime.engine.connect() as connection:
            preserved_catalog = {
                table: connection.execute(
                    text(f"SELECT * FROM app.{table} ORDER BY id")
                ).all()
                for table in ("catalog_items", "equipment_models")
            }
            assert len(preserved_catalog["catalog_items"]) == 4
            assert len(preserved_catalog["equipment_models"]) == 1
        command.upgrade(config, "0003_mvp008_orders")
        # A complete synthetic pre-MVP-019 request, snapshot and outbox survive.
        with runtime.unit_of_work() as session:
            session.add(
                ConsentText(
                    version="SYNTHETIC-MVP019-UPGRADE",
                    purpose="SYNTHETIC TEST ONLY",
                    text="Not legally approved",
                    checksum="synthetic-upgrade",
                    active_from=utc_now(),
                )
            )
            session.flush()
            consent = Consent(text_version="SYNTHETIC-MVP019-UPGRADE")
            session.add(consent)
            session.flush()
            order = Order(
                kind="commercial",
                consent_id=consent.id,
                idempotency_key=uuid4(),
                contact_name="Synthetic upgrade",
                email="synthetic@example.invalid",
                request_hash="synthetic",
                receipt_token_hash="synthetic",
                known_subtotal=Decimal("0"),
                delivery_status="not_required",
                delivery_cost=Decimal("0"),
            )
            session.add(order)
            session.flush()
            item_id = session.scalar(text("SELECT id FROM app.catalog_items LIMIT 1"))
            session.add(
                OrderItem(
                    order_id=order.id,
                    item_id=item_id,
                    qty=1,
                    sku_snapshot="SYNTHETIC-UPGRADE",
                    name_snapshot="Synthetic upgrade",
                    kind_snapshot="product",
                    unit_snapshot="unit",
                    price_mode_snapshot="on_request",
                    currency_snapshot="RUB",
                    tax_note_snapshot="Synthetic",
                    requires_shipping_snapshot=False,
                    catalog_revision_snapshot=7,
                )
            )
            session.add(EmailOutbox(order_id=order.id))
        with runtime.engine.connect() as connection:
            preserved_orders = {
                table: connection.execute(
                    text(f"SELECT * FROM app.{table} ORDER BY 1")
                ).all()
                for table in (
                    "consent_texts",
                    "consents",
                    "orders",
                    "order_items",
                    "email_outbox",
                )
            }
            # Catalog structure, indexes, and constraints must be unchanged;
            # equipment_models gets exactly one nullable column and one FK.
            old_columns = connection.execute(
                text(
                    "SELECT table_name,column_name,data_type,is_nullable,"
                    "column_default "
                    "FROM information_schema.columns WHERE table_schema='app' "
                    "ORDER BY table_name,ordinal_position"
                )
            ).all()
        with runtime.engine.connect() as connection:
            schema_sql = text(
                "SELECT 'index',indexname,indexdef FROM pg_indexes "
                "WHERE schemaname='app' UNION ALL "
                "SELECT 'constraint',c.conname,pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace "
                "WHERE n.nspname='app' ORDER BY 1,2,3"
            )
            old_schema = connection.execute(schema_sql).all()
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        with migrator.engine.begin() as connection:
            new_columns = connection.execute(
                text(
                    "SELECT table_name,column_name,data_type,is_nullable,"
                    "column_default "
                    "FROM information_schema.columns WHERE table_schema='app' "
                    "AND table_name NOT IN "
                    "('documents','document_versions','document_sections') "
                    "AND column_name <> 'manual_document_id' "
                    "ORDER BY table_name,ordinal_position"
                )
            ).all()
            assert new_columns == old_columns
            new_schema = connection.execute(schema_sql).all()
            # Only the three new tables' objects and the manual FK may appear.
            old_names = {row[1] for row in old_schema}
            assert [row for row in new_schema if row[1] in old_names] == old_schema
            added_names = {row[1] for row in new_schema} - old_names
            assert all(
                name.startswith(
                    (
                        "pk_document",
                        "uq_document",
                        "ck_document",
                        "fk_document",
                        "ix_document",
                    )
                )
                or name == "fk_equipment_models_manual_document_id_documents"
                for name in added_names
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM app.equipment_models "
                        "WHERE manual_document_id IS NOT NULL"
                    )
                )
                == 0
            )
            for table, rows in (
                preserved_staff | preserved_catalog | preserved_orders
            ).items():
                columns = ",".join(rows[0]._mapping.keys()) if rows else "*"
                assert (
                    connection.execute(
                        text(f"SELECT {columns} FROM app.{table} ORDER BY 1")
                    ).all()
                    == rows
                )
            assert connection.scalar(
                text("SELECT amount FROM app.integration_probe")
            ) == Decimal("123.45")
            connection.execute(text("DELETE FROM app.integration_probe"))
            # Only this sentinel-guarded disposable stack's migrator cleans up
            # the synthetic setup; production/runtime cannot erase audit/users.
            connection.execute(text("DELETE FROM app.orders"))
            connection.execute(text("DELETE FROM app.consents"))
            connection.execute(text("DELETE FROM app.catalog_items"))
            connection.execute(text("DELETE FROM app.equipment_models"))
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
