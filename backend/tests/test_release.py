"""Release modes are enforced before body validation and domain side effects."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.release import release_modes
from app.main import app
from app.modules.kb.files import FilePipeline, FileRejected


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
