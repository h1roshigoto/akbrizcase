"""Artificial PG16 binding/rollback tests; real full-profile guards run separately.

Only the target adapter is replaced with the existing integration sentinel. No
authentication, UoW, catalog mutation or audit behavior is mocked. The conftest's
immutable synthetic consent is masked only in the fresh-target observation. The no-image
fixture intentionally leaves AV/HTTPS acceptance to the full-profile rehearsal.
"""

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, insert, select, text
from sqlalchemy.orm import Session

from app.core.db import Database
from app.core.staff import bootstrap_admin
from app.modules.catalog import developer_pack as module
from app.modules.catalog.images.pipeline import ImagePipeline
from app.modules.catalog.models import CatalogItem, EquipmentModel

pytestmark = pytest.mark.integration
LOGIN = "artificial-pack-admin"
PASSWORD = "artificial-pack-admin-password"


def write_pack(root: Path) -> dict[str, Any]:
    (root / "images").mkdir()
    catalog = {
        "catalog_items": [
            {
                "id": str(uuid4()),
                "sku": "ARTIFICIAL-PG-PACK",
                "kind": "service",
                "name": "Artificial PG pack",
                "description": "Synthetic only",
                "attributes": {},
                "unit": "unit",
                "price_mode": "fixed",
                "price": "123.45",
                "currency": "RUB",
                "tax_note": "Synthetic only",
                "requires_shipping": False,
                "revision": 19,
                "status": "published",
                "image_file": None,
            }
        ],
        "equipment_models": [
            {
                "id": str(uuid4()),
                "code": "ARTIFICIAL-PACK-MODEL",
                "name": "Artificial",
                "description": "Synthetic only",
                "status": "published",
            }
        ],
        "catalog_item_models": [],
    }
    data = json.dumps(catalog).encode()
    (root / "catalog.json").write_bytes(data)
    manifest = {
        "format": 1,
        "classification": "authorized-development/non-production",
        "pack_id": str(uuid4()),
        "schema_revision": "0011_mvp039_http_security",
        "provenance": {
            "source_code_commit": "a" * 40,
            "selective_export_sha256": "b" * 64,
            "source_report_sha256": "c" * 64,
            "authorization": "owner-authorized-private-development",
            "authorization_reference": "SYNTHETIC-TEST-ONLY",
        },
        "counts": {"catalog_items": 1, "images": 0, "equipment_models": 1},
        "files": {"catalog.json": hashlib.sha256(data).hexdigest()},
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return catalog


@pytest.fixture
def pack_database(
    databases: tuple[Database, Database],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Database]:
    runtime, migrator = databases
    assert os.environ["DB_INTEGRATION_ISOLATED"] == "1"
    assert os.environ["DB_HOST"] == "postgres-test"
    # The shared integration conftest can retain exactly the public seed's four
    # artificial cards and one artificial model. Borrow those fixture slots only
    # after proving their identity; restore every original field/UUID afterwards.
    # No importer or operational command performs this test-only preparation.
    with migrator.engine.begin() as connection:
        original_items = [
            dict(row)
            for row in connection.execute(select(CatalogItem.__table__)).mappings()
        ]
        original_models = [
            dict(row)
            for row in connection.execute(select(EquipmentModel.__table__)).mappings()
        ]
        assert {row["sku"] for row in original_items} <= {
            "DEMO-P-001",
            "DEMO-P-002",
            "DEMO-P-003",
            "DEMO-S-001",
        }
        assert all(row["attributes"].get("demo_only") is True for row in original_items)
        assert {row["code"] for row in original_models} <= {"DEMO-MODEL-001"}
        for table in (
            "staff_users",
            "orders",
            "documents",
            "catalog_item_models",
            "chat_sessions",
            "diag_trees",
        ):
            assert connection.scalar(text(f"SELECT count(*) FROM app.{table}")) == 0
        connection.execute(delete(CatalogItem))
        connection.execute(delete(EquipmentModel))

    def target(session: Session) -> None:
        assert (
            session.scalar(
                text(
                    "SELECT shobj_description(oid,'pg_database') FROM pg_database "
                    "WHERE datname=current_database()"
                )
            )
            == "akbrizcase:synthetic-seed:test"
        )

    original_scalar = Session.scalar

    def observed_scalar(
        self: Session, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if str(statement) == "SELECT count(*) FROM app.consent_texts":
            versions = list(self.scalars(text("SELECT version FROM app.consent_texts")))
            assert all(version.startswith("SYNTHETIC-") for version in versions)
            return 0
        return original_scalar(self, statement, *args, **kwargs)

    # Never delete or alter the immutable predecessor consent fixture. The
    # separate actual full-profile rehearsal exercises the unmodified guard.
    monkeypatch.setattr(Session, "scalar", observed_scalar)
    monkeypatch.setattr(module, "_environment", lambda project: None)
    monkeypatch.setattr(module, "_target", target)
    try:
        with runtime.unit_of_work() as session:
            assert bootstrap_admin(session, login=LOGIN, password=PASSWORD).ok
        yield runtime
    finally:
        # Only this conftest's sentinel-checked disposable database; no source
        # connection exists; restore the borrowed artificial seed rows exactly.
        with migrator.engine.begin() as connection:
            for table in (
                "audit_events",
                "catalog_items",
                "equipment_models",
                "staff_users",
            ):
                connection.execute(text(f"DELETE FROM app.{table}"))
            if original_models:
                connection.execute(insert(EquipmentModel), original_models)
            if original_items:
                connection.execute(insert(CatalogItem), original_items)


def run(database: Database, root: Path) -> dict[str, object]:
    return module.import_pack(
        database,
        root,
        project="artificial-integration",
        login=LOGIN,
        password=PASSWORD,
        pipeline=ImagePipeline(Path(os.environ["APP_FILES_DIR"])),
    )


def snapshot(database: Database) -> dict[str, list[object]]:
    with database.unit_of_work() as session:
        return {
            table: list(
                session.scalars(
                    text(f"SELECT row_to_json(t) FROM app.{table} t ORDER BY id")
                )
            )
            for table in ("catalog_items", "equipment_models", "audit_events")
        }


def test_preserved_values_and_read_only_repeat(
    pack_database: Database, tmp_path: Path
) -> None:
    catalog = write_pack(tmp_path)
    assert run(pack_database, tmp_path)["code"] == "IMPORTED"
    before = snapshot(pack_database)
    item = before["catalog_items"][0]
    assert isinstance(item, dict)
    assert item["id"] == catalog["catalog_items"][0]["id"]
    assert item["sku"] == "ARTIFICIAL-PG-PACK"
    assert str(item["price"]) == "123.45"
    assert item["revision"] == 19 and item["status"] == "published"
    assert item["requires_shipping"] is False
    assert run(pack_database, tmp_path)["code"] == "ALREADY_IMPORTED"
    assert snapshot(pack_database) == before


def test_mid_operation_failure_rolls_back_and_can_retry(
    pack_database: Database, tmp_path: Path
) -> None:
    write_pack(tmp_path)
    before = snapshot(pack_database)

    def injected(session: Session, context: object) -> None:
        if any(isinstance(obj, CatalogItem) for obj in session.new):
            raise RuntimeError("SYNTHETIC_INJECTED_FAILURE")

    event.listen(Session, "after_flush", injected)
    try:
        with pytest.raises(RuntimeError, match="SYNTHETIC_INJECTED_FAILURE"):
            run(pack_database, tmp_path)
    finally:
        event.remove(Session, "after_flush", injected)
    assert snapshot(pack_database) == before
    assert run(pack_database, tmp_path)["code"] == "IMPORTED"


@pytest.mark.parametrize("field", ["id", "sku"])
def test_conflict_never_overwrites(
    pack_database: Database, tmp_path: Path, field: str
) -> None:
    catalog = write_pack(tmp_path)
    assert run(pack_database, tmp_path)["code"] == "IMPORTED"
    before = snapshot(pack_database)
    catalog["catalog_items"][0][field] = (
        str(uuid4()) if field == "id" else "ARTIFICIAL-OTHER"
    )
    data = json.dumps(catalog).encode()
    (tmp_path / "catalog.json").write_bytes(data)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["files"]["catalog.json"] = hashlib.sha256(data).hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(module.PackRefused, match="TARGET_CONFLICT"):
        run(pack_database, tmp_path)
    assert snapshot(pack_database) == before
