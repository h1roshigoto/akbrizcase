"""Catalog invariants are exercised against PostgreSQL, bypassing DTO validation."""

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.db import Database
from app.core.db_types import Base
from app.modules.catalog.models import CatalogItem, EquipmentModel
from app.modules.catalog.seed import SeedRefused, seed_catalog

pytestmark = pytest.mark.integration


@pytest.fixture
def catalog_database(databases: tuple[Database, Database]) -> Iterator[Database]:
    runtime, migrator = databases
    try:
        yield runtime
    finally:
        with migrator.unit_of_work() as session:
            session.execute(text("DELETE FROM app.catalog_items"))
            session.execute(text("DELETE FROM app.equipment_models"))


def item_values(**changes: object) -> dict[str, object]:
    return {
        "sku": "TEST-SKU",
        "kind": "product",
        "name": "Синтетическая тестовая позиция",
        "description": "Только тест",
        "unit": "шт.",
        "price_mode": "fixed",
        "price": Decimal("123.45"),
        "tax_note": "ТЕСТ",
        **changes,
    }


@pytest.mark.parametrize("kind", ["product", "service"])
@pytest.mark.parametrize("price", [Decimal("0.00"), Decimal("123.45"), None])
@pytest.mark.parametrize("status", ["draft", "published", "archived"])
def test_valid_items(
    catalog_database: Database, kind: str, price: Decimal | None, status: str
) -> None:
    with catalog_database.unit_of_work() as session:
        identifier = session.scalar(
            insert(CatalogItem)
            .values(
                **item_values(
                    kind=kind,
                    price=price,
                    price_mode="on_request" if price is None else "fixed",
                    requires_shipping=kind == "product",
                    status=status,
                    attributes={"test": True, "nested": {"only_display": [1, "x"]}},
                )
            )
            .returning(CatalogItem.id)
        )
    with catalog_database.unit_of_work() as session:
        item = session.get(CatalogItem, identifier)
        assert item is not None
        assert isinstance(item.id, UUID) and item.id.version == 4
        assert item.price == price
        assert price is None or isinstance(item.price, Decimal)
        assert item.currency == "RUB" and item.revision == 1
        assert item.image_key is None and item.status == status
        assert item.attributes["test"] is True
        assert item.created_at.utcoffset() is not None
        assert item.created_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"price": Decimal("-0.01")}, "price"),
        ({"price": Decimal("NaN")}, "price"),
        ({"price": None}, "price"),
        ({"price_mode": "on_request"}, "price"),
        # Both price checks reject this row; PostgreSQL may report either first.
        ({"price_mode": "invalid", "price": None}, "price"),
        ({"kind": "service", "requires_shipping": True}, "shipping"),
        ({"revision": 0}, "revision"),
        ({"revision": -1}, "revision"),
        ({"kind": "invalid"}, "kind"),
        ({"kind": ""}, "kind"),
        ({"status": "invalid"}, "status"),
        ({"status": ""}, "status"),
        ({"currency": "USD"}, "currency"),
        ({"currency": "rub"}, "currency"),
        ({"currency": ""}, "currency"),
        ({"attributes": ["not an object"]}, "attributes_object"),
    ],
)
def test_invalid_items_rejected_by_postgres(
    catalog_database: Database, changes: dict[str, object], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=f"ck_catalog_items_{constraint}"):
        with catalog_database.unit_of_work() as session:
            session.execute(insert(CatalogItem).values(**item_values(**changes)))


@pytest.mark.parametrize(
    "column", ["kind", "status", "currency", "revision", "requires_shipping"]
)
def test_required_values_cannot_be_null(
    catalog_database: Database, column: str
) -> None:
    with pytest.raises(IntegrityError):
        with catalog_database.unit_of_work() as session:
            session.execute(insert(CatalogItem).values(**item_values(**{column: None})))


def test_money_precision_and_overflow(catalog_database: Database) -> None:
    with catalog_database.unit_of_work() as session:
        session.execute(
            insert(CatalogItem).values(**item_values(price=Decimal("9999999999.99")))
        )
    with pytest.raises(DBAPIError):
        with catalog_database.unit_of_work() as session:
            session.execute(
                insert(CatalogItem).values(
                    **item_values(sku="OVERFLOW", price=Decimal("10000000000.00"))
                )
            )
    with catalog_database.engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT data_type, numeric_precision, numeric_scale "
                "FROM information_schema.columns WHERE table_schema='app' "
                "AND table_name='catalog_items' AND column_name='price'"
            )
        ).one() == ("numeric", 12, 2)


def test_duplicate_sku_and_invalid_update(catalog_database: Database) -> None:
    with catalog_database.unit_of_work() as session:
        session.execute(insert(CatalogItem).values(**item_values()))
    with pytest.raises(IntegrityError, match="uq_catalog_items_sku"):
        with catalog_database.unit_of_work() as session:
            session.execute(insert(CatalogItem).values(**item_values()))
    with pytest.raises(IntegrityError, match="ck_catalog_items_price"):
        with catalog_database.unit_of_work() as session:
            session.execute(text("UPDATE app.catalog_items SET price = -1"))


@pytest.mark.parametrize("status", ["draft", "published", "archived"])
def test_equipment_constraints(catalog_database: Database, status: str) -> None:
    values = dict(
        code="TEST-MODEL",
        name="ТЕСТ: модель",
        description="Синтетическая",
        status=status,
    )
    with catalog_database.unit_of_work() as session:
        session.execute(insert(EquipmentModel).values(**values))
    with pytest.raises(IntegrityError, match="uq_equipment_models_code"):
        with catalog_database.unit_of_work() as session:
            session.execute(insert(EquipmentModel).values(**values))
    with pytest.raises(IntegrityError, match="ck_equipment_models_status"):
        with catalog_database.unit_of_work() as session:
            session.execute(
                insert(EquipmentModel).values(
                    **{**values, "code": "OTHER", "status": "invalid"}
                )
            )


def test_indexes_and_schema_match_models(catalog_database: Database) -> None:
    with catalog_database.engine.connect() as connection:
        indexes = dict(
            connection.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='app' "
                    "AND tablename IN ('catalog_items', 'equipment_models')"
                )
            )
            .tuples()
            .all()
        )
        for column in ("name", "sku"):
            definition = indexes[f"ix_catalog_items_{column}_trgm"]
            assert f"USING gin ({column} gin_trgm_ops)" in definition
        partial = indexes["ix_catalog_items_published_kind"]
        assert "USING btree (kind)" in partial
        assert "WHERE" in partial and "published" in partial and "status" in partial
        for name in ("uq_catalog_items_sku", "uq_equipment_models_code"):
            assert "UNIQUE INDEX" in indexes[name] and "USING btree" in indexes[name]
        # Actual planner use verifies the operator class works for substring search.
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        for column in ("name", "sku"):
            plan = " ".join(
                connection.scalars(
                    text(
                        "EXPLAIN SELECT id FROM app.catalog_items "
                        f"WHERE {column} ILIKE '%DEMO%'"
                    )
                )
            )
            assert f"ix_catalog_items_{column}_trgm" in plan
        # Reflect explicit app FKs consistently with MVP-004's metadata check.
        # With app in search_path PostgreSQL otherwise omits the target schema.
        connection.execute(text("SET LOCAL search_path TO public"))
        previous_schema = connection.dialect.default_schema_name
        connection.dialect.default_schema_name = "public"
        try:
            context = MigrationContext.configure(
                connection,
                opts={
                    "include_schemas": True,
                    "include_object": lambda obj, name, type_, reflected, compare_to: (
                        not (
                            type_ == "table"
                            and name in {"integration_probe", "alembic_version"}
                        )
                    ),
                },
            )
            assert compare_metadata(context, Base.metadata) == []
        finally:
            connection.dialect.default_schema_name = previous_schema


def test_seed_idempotence_and_repeat_migration(catalog_database: Database) -> None:
    seed_catalog(catalog_database, mode="test")
    with catalog_database.unit_of_work() as session:
        rows = session.scalars(select(CatalogItem).order_by(CatalogItem.sku)).all()
        assert len(rows) == 4
        assert {row.kind for row in rows} == {"product", "service"}
        assert {row.price_mode for row in rows} == {"fixed", "on_request"}
        assert any(row.requires_shipping for row in rows)
        assert all(row.price is None for row in rows if row.price_mode == "on_request")
        assert all(not row.requires_shipping for row in rows if row.kind == "service")
        assert all("ДЕМО" in row.name and row.image_key is None for row in rows)
        rows[0].revision = 7
        rows[0].status = "archived"
        original = [
            (row.id, row.sku, row.price, row.revision, row.status) for row in rows
        ]
        model = session.scalars(select(EquipmentModel)).one()
        model_id = model.id
    seed_catalog(catalog_database, mode="test")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with catalog_database.unit_of_work() as session:
        rows = session.scalars(select(CatalogItem).order_by(CatalogItem.sku)).all()
        assert [
            (row.id, row.sku, row.price, row.revision, row.status) for row in rows
        ] == original
        assert session.scalars(select(EquipmentModel.id)).all() == [model_id]


@pytest.mark.parametrize("mode", ["demo", "production"])
def test_seed_cannot_target_wrong_database_marker(
    catalog_database: Database, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", mode)
    with pytest.raises(SeedRefused):
        seed_catalog(catalog_database, mode=mode)
    with catalog_database.unit_of_work() as session:
        assert session.scalars(select(CatalogItem)).all() == []
        assert session.scalars(select(EquipmentModel)).all() == []


def test_runtime_cannot_enable_seed_for_production(catalog_database: Database) -> None:
    with pytest.raises(DBAPIError):
        with catalog_database.unit_of_work() as session:
            session.execute(
                text(
                    "COMMENT ON DATABASE akbrizcase IS 'akbrizcase:synthetic-seed:demo'"
                )
            )


def test_seed_rolls_back_whole_set(
    databases: tuple[Database, Database], catalog_database: Database
) -> None:
    _, migrator = databases
    with migrator.unit_of_work() as session:
        session.execute(
            text(
                "ALTER TABLE app.catalog_items ADD CONSTRAINT seed_failure "
                "CHECK (sku <> 'DEMO-P-002')"
            )
        )
    try:
        with pytest.raises(IntegrityError, match="seed_failure"):
            seed_catalog(catalog_database, mode="test")
        with catalog_database.unit_of_work() as session:
            assert session.scalars(select(CatalogItem)).all() == []
            assert session.scalars(select(EquipmentModel)).all() == []
    finally:
        with migrator.unit_of_work() as session:
            session.execute(
                text("ALTER TABLE app.catalog_items DROP CONSTRAINT seed_failure")
            )
