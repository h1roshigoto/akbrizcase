"""Entirely artificial offline developer-pack attacks and field invariants."""

import hashlib
import io
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from PIL import Image

from app.modules.catalog.developer_pack import PackRefused, _environment, load_pack


def fixture_pack(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    root.mkdir(exist_ok=True)
    (root / "images").mkdir(exist_ok=True)
    identifier = str(uuid4())
    image = f"images/{identifier}.png"
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), (20, 40, 60)).save(stream, "PNG")
    (root / image).write_bytes(stream.getvalue())
    item = {
        "id": identifier,
        "sku": "ARTIFICIAL-PACK-P",
        "kind": "product",
        "name": "Artificial fixture",
        "description": "Synthetic only",
        "attributes": {"synthetic_only": True},
        "unit": "unit",
        "price_mode": "fixed",
        "price": "123.45",
        "currency": "RUB",
        "tax_note": "Synthetic",
        "requires_shipping": True,
        "revision": 7,
        "status": "published",
        "image_file": image,
    }
    catalog = {
        "catalog_items": [
            item,
            {
                **item,
                "id": str(uuid4()),
                "sku": "ARTIFICIAL-PACK-S",
                "kind": "service",
                "price_mode": "on_request",
                "price": None,
                "requires_shipping": False,
                "status": "draft",
                "image_file": None,
            },
        ],
        "equipment_models": [
            {
                "id": str(uuid4()),
                "code": "ARTIFICIAL-MODEL",
                "name": "Artificial model",
                "description": "Synthetic only",
                "status": "published",
            }
        ],
        "catalog_item_models": [],
    }
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
        "counts": {"catalog_items": 2, "images": 1, "equipment_models": 1},
        "files": {},
    }
    save(root, manifest, catalog)
    return manifest, catalog


def save(root: Path, manifest: dict[str, Any], catalog: dict[str, Any]) -> None:
    data = json.dumps(catalog, ensure_ascii=False).encode()
    (root / "catalog.json").write_bytes(data)
    manifest["files"] = {
        "catalog.json": hashlib.sha256(data).hexdigest(),
        **{
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (root / "images").iterdir()
            if p.is_file()
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest))


def test_decimal_identity_and_noncommercial_fields(tmp_path: Path) -> None:
    fixture_pack(tmp_path)
    pack = load_pack(tmp_path)
    assert str(pack.catalog.catalog_items[0].price) == "123.45"
    assert pack.catalog.catalog_items[0].revision == 7
    assert pack.catalog.catalog_items[1].price is None
    assert not pack.catalog.catalog_items[1].requires_shipping
    assert pack.catalog.catalog_items[1].status == "draft"


@pytest.mark.parametrize(
    "attack",
    [
        "duplicate_uuid",
        "duplicate_sku",
        "bad_uuid",
        "negative_price",
        "float_price",
        "nan_price",
        "precision_loss",
        "price_mode",
        "service_shipping",
        "status",
        "kb_reference",
        "staff",
        "orders",
        "secrets",
        "nested_secret",
        "traversal",
        "counts",
        "format",
        "boolean_format",
        "model_reference",
        "image_binding",
    ],
)
def test_reject_mutations(tmp_path: Path, attack: str) -> None:
    manifest, catalog = fixture_pack(tmp_path)
    first, second = catalog["catalog_items"]
    if attack == "duplicate_uuid":
        second["id"] = first["id"]
    elif attack == "duplicate_sku":
        second["sku"] = first["sku"]
    elif attack == "bad_uuid":
        first["id"] = "invalid"
    elif attack == "negative_price":
        first["price"] = "-1.00"
    elif attack == "float_price":
        first["price"] = 123.45
    elif attack == "nan_price":
        first["price"] = "NaN"
    elif attack == "precision_loss":
        first["price"] = "1.001"
    elif attack == "price_mode":
        first["price_mode"] = "on_request"
    elif attack == "service_shipping":
        second["requires_shipping"] = True
    elif attack == "status":
        first["status"] = "active"
    elif attack == "kb_reference":
        catalog["catalog_item_models"] = [{"evidence_section_id": str(uuid4())}]
    elif attack in {"staff", "orders", "secrets"}:
        catalog[attack] = []
    elif attack == "nested_secret":
        first["attributes"]["password_hash"] = "artificial"
    elif attack == "traversal":
        first["image_file"] = "../outside.jpg"
    elif attack == "counts":
        manifest["counts"]["catalog_items"] = 3
    elif attack == "format":
        manifest["format"] = 2
    elif attack == "boolean_format":
        manifest["format"] = True
    elif attack == "model_reference":
        catalog["equipment_models"][0]["manual_document_id"] = str(uuid4())
    elif attack == "image_binding":
        second["image_file"] = first["image_file"]
    save(tmp_path, manifest, catalog)
    with pytest.raises(PackRefused):
        load_pack(tmp_path)


@pytest.mark.parametrize(
    "attack",
    [
        "checksum",
        "corrupt",
        "missing",
        "extra",
        "symlink",
        "hardlink",
        "duplicate_json",
        "archive",
        "deep_json",
    ],
)
def test_reject_files(tmp_path: Path, attack: str) -> None:
    manifest, catalog = fixture_pack(tmp_path)
    image = tmp_path / catalog["catalog_items"][0]["image_file"]
    if attack == "checksum":
        manifest["files"]["catalog.json"] = "0" * 64
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    elif attack == "corrupt":
        image.write_bytes(b"corrupt")
    elif attack == "missing":
        image.unlink()
    elif attack == "extra":
        (tmp_path / "secret.txt").write_text("artificial")
    elif attack == "symlink":
        image.unlink()
        image.symlink_to(tmp_path / "catalog.json")
    elif attack == "hardlink":
        image.link_to(tmp_path / "alias.png") if hasattr(image, "link_to") else (
            tmp_path / "alias.png"
        ).hardlink_to(image)
    elif attack == "duplicate_json":
        p = tmp_path / "manifest.json"
        p.write_text(p.read_text().replace('"format": 1', '"format": 1, "format": 1'))
    elif attack == "deep_json":
        data = b"[" * 2000 + b"0" + b"]" * 2000
        (tmp_path / "catalog.json").write_bytes(data)
        manifest["files"]["catalog.json"] = hashlib.sha256(data).hexdigest()
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    elif attack == "archive":
        with pytest.raises(PackRefused):
            load_pack(image)
        return
    with pytest.raises(PackRefused):
        load_pack(tmp_path)


def test_environment_flag_alone_cannot_authorize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("RELEASE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("RELEASE_PROJECT", "akbriz-staging-0123456789ab")
    with pytest.raises(PackRefused):
        _environment("akbriz-staging-0123456789ab")


@pytest.mark.parametrize(
    "details",
    [
        {},
        {"pack_sha256": "a" * 64},
        {"pack_sha256": "a" * 64, "images_sha256": "bad"},
        {"pack_sha256": "a" * 64, "images_sha256": "b" * 64, "password": "artificial"},
    ],
)
def test_developer_audit_is_closed(details: dict[str, str]) -> None:
    from app.core.audit_schema import validate_audit

    with pytest.raises(ValueError, match="AUDIT_SCHEMA_INVALID"):
        validate_audit("catalog.developer_import", "developer_pack", "success", details)


def test_developer_audit_accepts_only_two_hashes() -> None:
    from app.core.audit_schema import validate_audit

    validate_audit(
        "catalog.developer_import",
        "developer_pack",
        "success",
        {
            "pack_sha256": "a" * 64,
            "images_sha256": "b" * 64,
        },
    )
