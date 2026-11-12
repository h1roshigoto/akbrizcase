"""Release modes are enforced before body validation and domain side effects."""

import json
import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app.core.db_migrate import failure_code
from app.core.release import FullReleaseModes, modes_fingerprint, release_modes
from app.main import app
from app.modules.kb.files import FilePipeline, FileRejected


def test_standalone_compose_mount_and_port_boundaries() -> None:
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "compose.release.yml").read_text()
    )
    services = config["services"]
    for name, service in services.items():
        if "tmpfs" in service:
            assert len(service["tmpfs"]) == 1
            assert service["tmpfs"][0].startswith("/tmp:rw,noexec,nosuid,")
        if name != "nginx":
            assert "ports" not in service
    assert services["nginx"]["ports"] == ["127.0.0.1:${RELEASE_PORT:?}:8443"]
    assert services["nginx"]["networks"] == ["ingress", "web"]
    assert config["networks"]["data"]["internal"] is True
    assert config["networks"]["web"]["internal"] is True
    assert all("external" not in volume for volume in config["volumes"].values())


def test_migration_failure_diagnostics_are_closed() -> None:
    class DriverError(Exception):
        sqlstate = "28P01"

    error = DBAPIError("SECRET SQL", {"password": "SECRET"}, DriverError("SECRET DSN"))
    assert failure_code(error) == "MIGRATION_AUTH_FAILED"
    assert failure_code(OSError("SECRET PATH")) == "MIGRATION_FILE_FAILED"
    assert failure_code(ValueError("SECRET INPUT")) == "MIGRATION_CONFIGURATION_FAILED"


def packet() -> dict[str, object]:
    return {
        "format": 1,
        "data": "synthetic-only",
        "catalog": True,
        "orders": False,
        "chat": False,
        "knowledge": False,
        "files": False,
        "smtp": "disabled",
        "clamav": "disabled-files-blocked",
        "signature_update": "disabled-files-blocked",
        "real_pii": False,
    }


def test_release_direct_http_and_missing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    path = tmp_path / "modes.json"
    path.write_text(json.dumps(packet()))
    monkeypatch.setenv("RELEASE_CONFIG_FILE", str(path))
    with TestClient(app) as client:
        for method, url in (
            ("POST", "/api/v1/orders"),
            ("POST", "/api/v1/orders/"),
            ("POST", "/api/v1/chat/sessions"),
            ("GET", "/api/v1/chat/scenarios"),
            ("GET", "/api/v1/documents"),
            ("GET", "/api/v1/knowledge/search"),
            ("POST", "/admin/catalog/items/anything/image"),
        ):
            response = client.request(method, url, json={"enabled": True})
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "RELEASE_MODE_DISABLED"
            assert response.headers["cache-control"] == "no-store"
            assert "script-src 'self'" in response.headers["content-security-policy"]
        with pytest.raises(FileRejected):
            FilePipeline.from_environment()
        path.unlink()
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        assert client.get("/api/v1/catalog/items").status_code == 503


@pytest.mark.parametrize(
    "field,value",
    [
        ("orders", True),
        ("chat", True),
        ("files", True),
        ("real_pii", True),
        ("smtp", "required"),
        ("clamav", "required"),
        ("signature_update", "current"),
        ("catalog", "true"),
    ],
)
def test_unsupported_release_modes_fail_closed(
    field: str, value: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "modes.json"
    path.write_text(json.dumps(packet() | {field: value}))
    monkeypatch.setenv("RELEASE_CONFIG_FILE", str(path))
    with pytest.raises(ValueError, match="^RELEASE_CONFIG_INVALID$"):
        release_modes()


def test_disabled_smtp_cannot_be_enabled_by_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "modes.json"
    path.write_text(json.dumps(packet()))
    monkeypatch.setenv("RELEASE_CONFIG_FILE", str(path))
    monkeypatch.setenv("SMTP_HOST", "synthetic.invalid")
    with pytest.raises(ValueError, match="^RELEASE_CONFIG_INVALID$"):
        release_modes()


def full_packet() -> dict[str, object]:
    return packet() | {
        "format": 2,
        "profile": "full-synthetic-staging",
        "orders": True,
        "chat": True,
        "knowledge": True,
        "files": True,
        "smtp": "isolated-sink",
        "clamav": "required",
        "signature_update": "operator-verified-offline",
    }


@pytest.fixture
def full_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "modes.json"
    config.write_text(json.dumps(full_packet()))
    authority = tmp_path / "authority.json"
    authority.write_text(
        json.dumps(
            {
                "format": 1,
                "scope": "local-synthetic-rehearsal",
                "project": "akbriz-staging-" + "a" * 12,
                "profile": "full-synthetic-staging",
                "modes": modes_fingerprint(full_packet()),
            }
        )
    )
    authority.chmod(0o444)
    # Model the distinct host owner / container UID without requiring root in units.
    monkeypatch.setattr(os, "geteuid", lambda: authority.stat().st_uid + 1)
    for key, value in {
        "APP_ENV": "staging",
        "RELEASE_CONFIG_FILE": str(config),
        "RELEASE_AUTHORITY_FILE": str(authority),
        "RELEASE_PROJECT": "akbriz-staging-" + "a" * 12,
        "DB_HOST": "postgres",
        "DB_NAME": "akbrizcase",
    }.items():
        monkeypatch.setenv(key, value)
    return config


def test_full_contract_and_ambient_smtp_denial(
    full_authority: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert isinstance(release_modes(), FullReleaseModes)
    monkeypatch.setenv("SMTP_HOST", "smtp-test")
    with pytest.raises(ValueError, match="^RELEASE_CONFIG_INVALID$"):
        release_modes()


@pytest.mark.parametrize(
    "key,value",
    [
        ("format", True),
        ("format", 2.0),
        ("orders", 1),
        ("real_pii", 0),
        ("real_pii", True),
        ("files", False),
        ("profile", "production"),
        ("extra", "ignored"),
        ("smtp", "external"),
    ],
)
def test_full_closed_schema(full_authority: Path, key: str, value: object) -> None:
    full_authority.write_text(json.dumps(full_packet() | {key: value}))
    with pytest.raises(ValueError, match="^RELEASE_CONFIG_INVALID$"):
        release_modes()


@pytest.mark.parametrize(
    "key,value",
    [
        ("APP_ENV", "demo"),
        ("APP_ENV", "test"),
        ("APP_ENV", "production"),
        ("RELEASE_PROJECT", "akbriz-staging-" + "b" * 12),
        ("RELEASE_AUTHORITY_FILE", "/nonexistent-authority"),
        ("DB_HOST", "external.invalid"),
        ("DB_NAME", "production"),
    ],
)
def test_full_requires_independent_controller_authority(
    full_authority: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="^RELEASE_CONFIG_INVALID$"):
        release_modes()


def test_duplicate_config_and_writable_authority_fail_closed(
    full_authority: Path,
) -> None:
    full_authority.write_text(json.dumps(full_packet())[:-1] + ',"format":2}')
    with pytest.raises(ValueError, match="^RELEASE_CONFIG_INVALID$"):
        release_modes()
    full_authority.write_text(json.dumps(full_packet()))
    authority = full_authority.with_name("authority.json")
    authority.chmod(0o644)
    with pytest.raises(ValueError, match="^RELEASE_CONFIG_INVALID$"):
        release_modes()


def test_full_marker_failure_closes_direct_database_and_file_paths(
    full_authority: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from app.core.release import validate_session
    from app.modules.catalog.images.pipeline import ImagePipeline

    session = MagicMock()
    session.scalar.return_value = "akbrizcase:synthetic-seed:test"
    with pytest.raises(ValueError, match="RELEASE_DATABASE_INVALID"):
        validate_session(session)
    session.scalar.return_value = "akbrizcase:synthetic-seed:demo"
    validate_session(session)
    monkeypatch.setenv("DB_PASSWORD_FILE", "/missing")
    for pipeline in (FilePipeline, ImagePipeline):
        with pytest.raises(ValueError):
            pipeline.from_environment()


def test_full_smtp_is_fixed_and_test_transport_stays_test_only(
    full_authority: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from app.modules.notify.smtp import SMTPConfigurationError, SMTPSettings

    monkeypatch.setenv("STAFF_TRUSTED_ORIGINS", "https://localhost:28443")
    monkeypatch.setattr("app.modules.notify.smtp.require_files", lambda: None)
    settings = SMTPSettings.from_environment()
    assert settings is not None and settings.mode == "staging-sink"
    assert settings.host == "smtp-test" and settings.port == 1025
    for change in (
        {"host": "external.invalid"},
        {"port": 25},
        {"sender": "real@example.com"},
        {"recipient": "real@example.com"},
        {"username": "secret", "password": "secret"},
        {"synthetic": False},
    ):
        with pytest.raises(SMTPConfigurationError):
            replace(settings, **change)
    monkeypatch.setenv("SMTP_TEST_DOUBLE", "1")
    with pytest.raises(SMTPConfigurationError):
        SMTPSettings.from_environment()


def test_concurrent_file_guards_construct_only_one_bounded_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from unittest.mock import Mock

    from app.core.release import _file_guard_pool, file_guard_database

    secret = tmp_path / "synthetic-password"
    secret.write_text("SYNTHETIC ONLY")
    monkeypatch.setenv("DB_PASSWORD_FILE", str(secret))
    pool = Mock()

    def construct(*args: object, **kwargs: object) -> object:
        # Yield long enough for simultaneous first callers to race the cache.
        Event().wait(0.05)
        return pool

    factory = Mock(side_effect=construct)
    monkeypatch.setattr("app.core.db.Database", factory)
    _file_guard_pool.cache_clear()
    try:
        with ThreadPoolExecutor(8) as executor:
            results = list(executor.map(lambda _: file_guard_database(), range(16)))
        assert all(result is pool for result in results)
        assert factory.call_count == 1
        assert factory.call_args.kwargs == {"release_guard": True}
    finally:
        _file_guard_pool.cache_clear()


def test_reduced_profile_denies_direct_smtp_sender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock
    from uuid import uuid4

    from app.modules.notify.outbox import Claim
    from app.modules.notify.smtp import SendFailure, SMTPSettings, send_notification

    config = tmp_path / "modes.json"
    config.write_text(json.dumps(packet()))
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("RELEASE_CONFIG_FILE", str(config))
    transport = Mock()
    monkeypatch.setattr("app.modules.notify.smtp.smtplib.SMTP", transport)
    settings = SMTPSettings(
        "127.0.0.1",
        1025,
        "test",
        "notify@example.invalid",
        "manager@example.invalid",
        "https://localhost:8443",
        frozenset({"https://localhost:8443"}),
        synthetic=True,
    )
    with pytest.raises(SendFailure) as failure:
        send_notification(settings, Claim(uuid4(), uuid4(), 1, uuid4(), 1))
    assert failure.value.code == "SMTP_UNAVAILABLE"
    transport.assert_not_called()
