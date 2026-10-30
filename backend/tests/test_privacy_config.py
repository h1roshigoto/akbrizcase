"""No sample setting or unchecked packet can silently authorize real collection."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.db_types import utc_now
from app.core.privacy import (
    PrivacyBundle,
    RetentionSettings,
    load_bundle,
    production_bundle,
)
from app.modules.orders.seed import demo_bundle


def test_explicit_synthetic_categories() -> None:
    settings = demo_bundle().retention
    assert settings.chat_access_hours == 24
    assert settings.chat_storage_days == 30
    assert settings.order_pii_after_terminal_days == 180
    assert settings.open_orders == settings.outbox_pending == "manual_review_no_ttl"
    assert settings.consent_evidence == "until_order_deleted"
    assert settings.consent_text == "immutable_permanent"


@pytest.mark.parametrize(
    "field,value",
    [
        ("chat_access_hours", 25),
        ("chat_storage_days", 0),
        ("audit_days", -1),
        ("technical_logs_days", "14"),
        ("backup_days", True),
        ("outbox_sent_days", None),
        ("outbox_failed_days", 1.5),
        ("deletion_journal_days", 13),
        ("backup_days", 15),
        ("linked_snapshots_after_terminal_days", 181),
        ("outbox_pending", "delete"),
        ("open_orders", "delete"),
        ("consent_evidence", "30"),
        ("consent_text", "30"),
        ("expert_review_after_content_retired_days", 0),
        ("publication_evidence_after_content_retired_days", 0),
        ("open_order_review_days", 0),
        ("outbox_pending_review_hours", 25),
    ],
)
def test_invalid_retention(field: str, value: object) -> None:
    data = demo_bundle().retention.model_dump() | {field: value}
    with pytest.raises(ValidationError):
        RetentionSettings.model_validate(data)


@pytest.mark.parametrize("field", list(RetentionSettings.model_fields))
def test_every_category_requires_an_explicit_decision(field: str) -> None:
    data = demo_bundle().retention.model_dump()
    del data[field]
    with pytest.raises(ValidationError):
        RetentionSettings.model_validate(data)


def test_production_config_missing_invalid_and_synthetic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("PRIVACY_CONFIG_FILE", raising=False)
    with pytest.raises(ValueError, match="unavailable"):
        production_bundle()
    path = tmp_path / "packet.json"
    monkeypatch.setenv("PRIVACY_CONFIG_FILE", str(path))
    for content in (
        "{}",
        demo_bundle().model_dump_json(),
        '{"approval_status":"APPROVED","approval_status":"APPROVED"}',
    ):
        path.write_text(content)
        with pytest.raises(ValueError):
            production_bundle()
    path.write_text(json.dumps({"unexpected": "private evidence"}))
    with pytest.raises(ValueError) as result:
        production_bundle()
    assert "private evidence" not in str(result.value)


def test_checksum_and_synthetic_approval_cannot_be_relabelled() -> None:
    for field in ("policy", "consent"):
        data = demo_bundle().model_dump(mode="json")
        data[field] = dict(data[field]) | {"checksum": "0" * 64}
        with pytest.raises(ValidationError):
            PrivacyBundle.model_validate(data)
    with pytest.raises(ValidationError):
        PrivacyBundle.model_validate(
            demo_bundle().model_dump() | {"approval_status": "APPROVED"}
        )
    bundle = demo_bundle().model_copy(
        update={"approved_at": utc_now() + timedelta(days=1)}
    )
    assert not bundle.valid_at(utc_now())


def test_duplicate_packet_fields_refused(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"approval_status":"APPROVED","approval_status":"SYNTHETIC ONLY"}')
    with pytest.raises(ValueError):
        load_bundle(path)


def test_read_only_cli_validation_needs_no_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.modules.orders.privacy_cli import main

    path = tmp_path / "approved.json"
    path.write_text(demo_bundle().model_dump_json())
    assert main(["check", "--bundle", str(path)]) == 0
    assert capsys.readouterr().out == "VALID_PACKET " + demo_bundle().checksum() + "\n"
    path.write_text('{"private-document":"must never be echoed"}')
    assert main(["check", "--bundle", str(path)]) == 1
    output = capsys.readouterr()
    assert not output.out and "must never be echoed" not in output.err


def test_blank_policy_version_refused_before_publication() -> None:
    data = demo_bundle().model_dump(mode="json")
    data["policy"]["version"] = "   "
    with pytest.raises(ValidationError):
        PrivacyBundle.model_validate(data)
