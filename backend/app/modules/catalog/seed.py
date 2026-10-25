"""Explicit, synthetic-only seed; never called by API, worker or migrations."""

import argparse
import os
import sys
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.core.db import Database, DatabaseConfigurationError, DatabaseSettings
from app.modules.catalog.models import CatalogItem, EquipmentModel


class SeedRefused(ValueError):
    """Safe refusal without connection details."""


def validate_environment(mode: str) -> None:
    if mode not in {"demo", "test"} or os.environ.get("APP_ENV") != mode:
        raise SeedRefused("Seed requires matching explicit demo/test mode and APP_ENV")


def seed_catalog(database: Database, *, mode: str) -> None:
    validate_environment(mode)
    with database.unit_of_work() as session:
        # Unlike a connection setting, a database comment cannot be changed by
        # the runtime role. Only the owner labels a disposable demo/test database.
        marker = session.scalar(
            text(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                "WHERE datname = current_database()"
            )
        )
        if marker != f"akbrizcase:synthetic-seed:{mode}":
            raise SeedRefused("Database is not explicitly marked for this seed mode")

        session.execute(
            insert(EquipmentModel)
            .values(
                code="DEMO-MODEL-001",
                name="ДЕМО: синтетическая модель оборудования",
                description=(
                    "Вымышленная модель для demo/test. Не реальное оборудование."
                ),
                status="published",
            )
            .on_conflict_do_nothing(index_elements=[EquipmentModel.code])
        )
        for sku, name, kind, price, shipping in (
            ("DEMO-P-001", "ДЕМО: учебный фильтр", "product", Decimal("123.45"), True),
            ("DEMO-P-002", "ДЕМО: учебный узел", "product", None, True),
            ("DEMO-P-003", "ДЕМО: учебный комплект", "product", Decimal("0.00"), False),
            (
                "DEMO-S-001",
                "ДЕМО: учебная консультация",
                "service",
                Decimal("67.89"),
                False,
            ),
        ):
            session.execute(
                insert(CatalogItem)
                .values(
                    sku=sku,
                    kind=kind,
                    name=name,
                    description=(
                        "Синтетический пример для demo/test. Цена вымышленная, "
                        "не является коммерческим предложением. "
                        "Совместимость с оборудованием не подтверждена."
                    ),
                    attributes={"demo_only": True, "материал": "вымышленный"},
                    unit="услуга" if kind == "service" else "шт.",
                    price_mode="fixed" if price is not None else "on_request",
                    price=price,
                    currency="RUB",
                    tax_note="ДЕМО: налоговый режим не определён",
                    requires_shipping=shipping,
                    image_key=None,
                    status="published",
                    revision=1,
                )
                .on_conflict_do_nothing(index_elements=[CatalogItem.sku])
            )
        # Commit belongs solely to the shared outer Unit of Work.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("demo", "test"))
    args = parser.parse_args()
    database = None
    try:
        # Refuse production before loading credentials or opening a connection.
        validate_environment(args.mode)
        database = Database(DatabaseSettings.from_environment())
        seed_catalog(database, mode=args.mode)
    except (SeedRefused, DatabaseConfigurationError) as error:
        sys.exit(str(error))
    except SQLAlchemyError:
        sys.exit("Seed failed; check schema and isolated demo/test configuration")
    finally:
        if database is not None:
            database.close()
    print("Synthetic catalog seed complete (existing SKU/code preserved)")


if __name__ == "__main__":
    main()
