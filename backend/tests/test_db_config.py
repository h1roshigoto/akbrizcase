"""Safe configuration and DB-independent liveness regression checks."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.db import DatabaseConfigurationError, DatabaseSettings
from app.main import app


def test_missing_configuration_keeps_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_PASSWORD_FILE", raising=False)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}


def test_password_file_and_separate_migration_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "synthetic_password"
    value = "synthetic@:/?#%password"
    path.write_text(value)
    monkeypatch.setenv("DB_PASSWORD_FILE", str(path))
    monkeypatch.delenv("MIGRATION_DB_PASSWORD_FILE", raising=False)
    settings = DatabaseSettings.from_environment()
    assert settings.user == "akbrizcase_runtime"
    assert settings.url().password == value
    assert value not in repr(settings)
    assert value not in str(settings.url())
    with pytest.raises(DatabaseConfigurationError) as failure:
        DatabaseSettings.from_environment(migration=True)
    assert str(failure.value) == "Database configuration unavailable"
