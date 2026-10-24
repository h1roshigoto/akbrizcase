"""Smoke checks that do not require a database or any credentials."""

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from pytest import MonkeyPatch


def test_application_imports_without_database_configuration() -> None:
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.main import app; "
            "from fastapi import FastAPI; "
            "assert isinstance(app, FastAPI); "
            "assert app.debug is False",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_healthz_without_database_configuration(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}
