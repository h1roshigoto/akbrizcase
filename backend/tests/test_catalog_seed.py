"""Seed guard must reject unsafe invocations before constructing a database."""

from unittest.mock import Mock

import pytest

from app.modules.catalog import seed


@pytest.mark.parametrize(
    "environment", [None, "production", "prod", "development", "test"]
)
def test_demo_refused_before_credentials_or_connection(
    environment: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if environment is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", environment)
    database = Mock()
    monkeypatch.setattr(seed, "Database", database)
    monkeypatch.setattr("sys.argv", ["seed", "--mode", "demo"])
    with pytest.raises(SystemExit, match="matching explicit demo/test"):
        seed.main()
    database.assert_not_called()


@pytest.mark.parametrize("mode", ["production", "prod", "", "DEMO"])
def test_production_mode_is_never_accepted(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", mode)
    with pytest.raises(seed.SeedRefused):
        seed.validate_environment(mode)
