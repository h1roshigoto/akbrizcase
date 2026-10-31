"""Destructive fixtures are restricted to the dedicated test container stack."""

import os
import secrets
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pytest import MonkeyPatch
from sqlalchemy import text

from app.core.db import Database, DatabaseSettings
from app.core.db_types import utc_now
from app.core.staff import bootstrap_admin, create_staff
from app.core.staff_models import StaffSession
from app.modules.catalog.seed import seed_catalog
from app.modules.orders.models import (
    Consent,
    ConsentText,
    OrderItem,
    OrderStatusHistory,
)


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
            # Raw SQL intentionally uses the predecessor's columns, not today's
            # Order mapping (which now also contains chat_session_id).
            order_id = uuid4()
            session.execute(
                text(
                    "INSERT INTO app.orders (id,kind,consent_id,idempotency_key,"
                    "contact_name,email,request_hash,receipt_token_hash,"
                    "known_subtotal,delivery_status,delivery_cost) VALUES "
                    "(:id,'commercial',:consent,:key,'Synthetic upgrade',"
                    "'synthetic@example.invalid','synthetic','synthetic',"
                    "0,'not_required',0)"
                ),
                {"id": order_id, "consent": consent.id, "key": uuid4()},
            )
            item_id = session.scalar(text("SELECT id FROM app.catalog_items LIMIT 1"))
            session.add(
                OrderItem(
                    order_id=order_id,
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
            session.execute(
                text("INSERT INTO app.email_outbox(id,order_id) VALUES (:id,:order)"),
                {"id": uuid4(), "order": order_id},
            )
            session.add(OrderStatusHistory(order_id=order_id, to_status="new"))
            support_consent = Consent(text_version="SYNTHETIC-MVP019-UPGRADE")
            session.add(support_consent)
            session.flush()
            support_id = uuid4()
            session.execute(
                text(
                    "INSERT INTO app.orders (id,kind,consent_id,idempotency_key,"
                    "contact_name,email,comment,request_hash,receipt_token_hash,"
                    "known_subtotal,delivery_status,delivery_cost) VALUES "
                    "(:id,'support',:consent,:key,'Synthetic support',"
                    "'synthetic@example.invalid','SYNTHETIC independent description',"
                    "'synthetic','synthetic',0,'not_required',0)"
                ),
                {"id": support_id, "consent": support_consent.id, "key": uuid4()},
            )
            session.execute(
                text("INSERT INTO app.email_outbox(id,order_id) VALUES (:id,:order)"),
                {"id": uuid4(), "order": support_id},
            )
            session.add(OrderStatusHistory(order_id=support_id, to_status="new"))
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
                    "order_status_history",
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
        command.upgrade(config, "0004_mvp019_documents")
        # Existing MVP-023 KB is preserved too; no synthetic applicability is
        # inferred by the migration from names, JSON or equipment manual links.
        with runtime.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO app.documents (id,code,title,kind,visibility) VALUES "
                    "('00000000-0000-4000-8000-000000000024','SYNTHETIC024UPGRADE',"
                    "'SYNTHETIC ONLY','other','internal')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO app.document_versions "
                    "(id,document_id,version,file_key,mime,size_bytes,checksum_sha256) "
                    "VALUES "
                    "('00000000-0000-4000-8000-000000000025',"
                    "'00000000-0000-4000-8000-000000000024',1,'SyntheticUpgrade024',"
                    "'text/plain',20,repeat('a',64))"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO app.document_sections "
                    "(id,document_version_id,section_key,title,text) VALUES "
                    "('00000000-0000-4000-8000-000000000026',"
                    "'00000000-0000-4000-8000-000000000025','intro','SYNTHETIC','SYNTHETIC')"
                )
            )
        with runtime.engine.connect() as connection:
            preserved_kb = {
                table: connection.execute(
                    text(f"SELECT * FROM app.{table} ORDER BY id")
                ).all()
                for table in ("documents", "document_versions", "document_sections")
            }
        command.upgrade(config, "0005_mvp024_applicability")
        with runtime.unit_of_work() as session:
            session.execute(
                text(
                    "INSERT INTO app.catalog_item_models "
                    "(item_id,model_id,evidence_section_id) "
                    "SELECT i.id,m.id,s.id FROM app.catalog_items i "
                    "CROSS JOIN app.equipment_models m "
                    "CROSS JOIN app.document_sections s LIMIT 1"
                )
            )
        with runtime.engine.connect() as connection:
            preserved_applicability = connection.execute(
                text("SELECT * FROM app.catalog_item_models ORDER BY item_id,model_id")
            ).all()
            assert len(preserved_applicability) == 1
        command.upgrade(config, "0006_mvp027_diagnostic_trees")
        # Verify exactly the immediate predecessor upgrades without changing any
        # existing rows, indexes, constraints, functions or tree lifecycle triggers.
        with runtime.engine.connect() as connection:
            before_0007 = connection.execute(schema_sql).all()
            tree_objects_sql = text(
                "SELECT pg_get_functiondef(oid) FROM pg_proc "
                "WHERE pronamespace='app'::regnamespace ORDER BY proname"
            )
            tree_functions = connection.execute(tree_objects_sql).all()
            triggers_sql = text(
                "SELECT pg_get_triggerdef(oid) FROM pg_trigger "
                "WHERE tgrelid IN ('app.diag_trees'::regclass,"
                "'app.diag_tree_sources'::regclass,'app.diag_tree_items'::regclass) "
                "AND NOT tgisinternal ORDER BY tgname"
            )
            tree_triggers = connection.execute(triggers_sql).all()
        command.upgrade(config, "0008_mvp037_privacy")
        # Genuine predecessor rows: known closed/cancelled, missing history and
        # conflicting history. No reset/downgrade or guessed updated_at.
        probes = []
        with migrator.engine.begin() as connection:
            for status, histories in (
                ("closed", 1),
                ("cancelled", 1),
                ("cancelled", 0),
                ("cancelled", 2),
            ):
                identifier, consent_id = uuid4(), uuid4()
                connection.execute(
                    text(
                        "INSERT INTO app.consents(id,text_version) VALUES "
                        "(:id,'SYNTHETIC-MVP019-UPGRADE')"
                    ),
                    {"id": consent_id},
                )
                at = connection.scalar(text("SELECT now()-interval '1 day'"))
                connection.execute(
                    text(
                        "INSERT INTO app.orders(id,kind,consent_id,idempotency_key,"
                        "contact_name,email,comment,request_hash,receipt_token_hash,"
                        "known_subtotal,delivery_status,delivery_cost,status,"
                        "created_at,closed_at) "
                        "VALUES (:id,'support',:consent,:key,'Synthetic upgrade',"
                        "'upgrade@example.invalid','Synthetic upgrade',"
                        "'synthetic','synthetic',"
                        "0,'not_required',0,:status,now()-interval '2 days',:closed)"
                    ),
                    {
                        "id": identifier,
                        "consent": consent_id,
                        "key": uuid4(),
                        "status": status,
                        "closed": at if status == "closed" else None,
                    },
                )
                for _ in range(histories):
                    connection.execute(
                        text(
                            "INSERT INTO app.order_status_history(id,order_id,"
                            "from_status,to_status,at) VALUES "
                            "(:id,:order,'in_progress',:status,:at)"
                        ),
                        {
                            "id": uuid4(),
                            "order": identifier,
                            "status": status,
                            "at": at,
                        },
                    )
                probes.append((identifier, consent_id, at if histories == 1 else None))
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        with migrator.engine.begin() as connection:
            for identifier, consent_id, expected in probes:
                assert (
                    connection.scalar(
                        text("SELECT terminal_at FROM app.orders WHERE id=:id"),
                        {"id": identifier},
                    )
                    == expected
                )
                connection.execute(
                    text("DELETE FROM app.orders WHERE id=:id"), {"id": identifier}
                )
                connection.execute(
                    text("DELETE FROM app.consents WHERE id=:id"), {"id": consent_id}
                )
        with runtime.engine.connect() as connection:
            after_0007 = connection.execute(schema_sql).all()
            old_names_0007 = {row[1] for row in before_0007}
            assert [
                row
                for row in after_0007
                if row[1] in old_names_0007 and row[1] != "ck_orders_source"
            ] == [row for row in before_0007 if row[1] != "ck_orders_source"]
            assert [
                row
                for row in connection.execute(tree_objects_sql).all()
                if "app.protect_consent_publication()" not in row[0]
                and "app.seed_demo_consent(" not in row[0]
                and "app.protect_deletion_entry()" not in row[0]
                and "app.check_erased_reasons()" not in row[0]
                and "app.protect_retention_identity()" not in row[0]
                and "app.protect_deletion_basis()" not in row[0]
            ] == tree_functions
            assert connection.execute(triggers_sql).all() == tree_triggers
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM app.orders "
                        "WHERE chat_session_id IS NOT NULL"
                    )
                )
                == 0
            )
        with migrator.engine.begin() as connection:
            new_columns = connection.execute(
                text(
                    "SELECT table_name,column_name,data_type,is_nullable,"
                    "column_default "
                    "FROM information_schema.columns WHERE table_schema='app' "
                    "AND table_name NOT IN "
                    "('documents','document_versions','document_sections',"
                    "'catalog_item_models','diag_trees',"
                    "'diag_tree_sources','diag_tree_items',"
                    "'chat_sessions','chat_messages','consent_publications',"
                    "'deletion_journal','cleanup_cursors','rate_limit_buckets') "
                    "AND column_name NOT IN ('manual_document_id','chat_session_id',"
                    "'terminal_at','failed_at') "
                    "ORDER BY table_name,ordinal_position"
                )
            ).all()
            assert new_columns == old_columns
            new_schema = connection.execute(schema_sql).all()
            # Only the planned additive objects and source enum may change.
            old_names = {row[1] for row in old_schema}
            assert [
                row
                for row in new_schema
                if row[1] in old_names and row[1] != "ck_orders_source"
            ] == [row for row in old_schema if row[1] != "ck_orders_source"]
            added_names = {row[1] for row in new_schema} - old_names
            assert all(
                name.startswith(
                    (
                        "pk_deletion_",
                        "fk_deletion_",
                        "uq_deletion_",
                        "ck_deletion_",
                        "pk_cleanup_",
                        "pk_rate_limit_",
                        "ck_rate_limit_",
                        "ix_rate_limit_",
                        "ix_orders_terminal_at",
                        "ck_orders_erased_fields",
                        "ck_email_outbox_failed_at",
                        "pk_consent_publications",
                        "fk_consent_publications",
                        "ck_consent_publications",
                        "pk_document",
                        "uq_document",
                        "ck_document",
                        "fk_document",
                        "ix_document",
                        "pk_catalog_item_models",
                        "ck_catalog_item_models",
                        "fk_catalog_item_models",
                        "ix_catalog_item_models",
                        "pk_diag_",
                        "uq_diag_",
                        "ck_diag_",
                        "fk_diag_",
                        "ix_diag_",
                        "pk_chat_",
                        "uq_chat_",
                        "ck_chat_",
                        "fk_chat_",
                        "ix_chat_",
                        "fk_orders_chat_session_id_",
                        "ix_orders_chat_session_id",
                    )
                )
                or name == "fk_equipment_models_manual_document_id_documents"
                or name == "erased_reasons"
                for name in added_names
            ), sorted(added_names)
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM app.equipment_models "
                        "WHERE manual_document_id IS NOT NULL"
                    )
                )
                == 0
            )
            assert (
                connection.execute(
                    text(
                        "SELECT * FROM app.catalog_item_models "
                        "ORDER BY item_id,model_id"
                    )
                ).all()
                == preserved_applicability
            )
            for table in ("diag_trees", "diag_tree_sources", "diag_tree_items"):
                assert connection.scalar(text(f"SELECT count(*) FROM app.{table}")) == 0
            for table, rows in preserved_kb.items():
                assert (
                    connection.execute(
                        text(f"SELECT * FROM app.{table} ORDER BY id")
                    ).all()
                    == rows
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
            connection.execute(text("DELETE FROM app.catalog_item_models"))
            connection.execute(text("DELETE FROM app.document_sections"))
            connection.execute(text("DELETE FROM app.document_versions"))
            connection.execute(text("DELETE FROM app.documents"))
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


@pytest.fixture(autouse=True)
def synthetic_ip_secret(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # Each test models an independent IP population, with a real runtime file.
    path = tmp_path / "ip-key"
    path.write_text(secrets.token_hex(32))
    path.chmod(0o600)
    monkeypatch.setenv("IP_HMAC_SECRET_FILE", str(path))
