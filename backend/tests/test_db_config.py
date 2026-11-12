"""Safe configuration and DB-independent liveness regression checks."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import QueuePool

from app.core.db import (
    API_WORKERS,
    Database,
    DatabaseConfigurationError,
    DatabaseSettings,
)
from app.core.timing_pool import TimedQueuePool
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


@pytest.mark.parametrize("timed", [False, True])
@pytest.mark.parametrize("api", [False, True])
@pytest.mark.parametrize("release_guard", [False, True])
def test_api_capacity_is_bounded_and_independent_of_timing(
    monkeypatch: pytest.MonkeyPatch, timed: bool, api: bool, release_guard: bool
) -> None:
    monkeypatch.setenv("PERF001_TIMING", "1" if timed else "0")
    database = Database(
        DatabaseSettings("127.0.0.1", 1, "synthetic", "synthetic", "synthetic"),
        api=api,
        release_guard=release_guard,
    )
    try:
        pool = database.engine.pool
        assert isinstance(pool, QueuePool)
        expected = (1, 0) if release_guard else ((8, 0) if api else (5, 5))
        assert (pool.size(), pool._max_overflow) == expected
        assert pool.timeout() == 3 and pool._pre_ping
        assert isinstance(pool, TimedQueuePool) is timed
        assert pool.checkedout() == 0
    finally:
        database.close()


def test_api_worker_count_matches_both_runtime_commands() -> None:
    import json

    import yaml

    root = Path(__file__).resolve().parents[2]
    commands = [
        yaml.safe_load((root / name).read_text())["services"]["api"]["command"]
        for name in ("docker-compose.yml", "compose.release.yml")
    ]
    commands.append(
        json.loads(
            next(
                line.removeprefix("CMD ")
                for line in (root / "backend/Dockerfile").read_text().splitlines()
                if line.startswith('CMD ["uvicorn"')
            )
        )
    )
    for command in commands:
        assert int(command[command.index("--workers") + 1]) == API_WORKERS == 3
        assert "--no-proxy-headers" in command and "--no-access-log" in command
