"""The owned scanner fixture uses the same worker count and full lifespan."""

import builtins
import runpy
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
import uvicorn
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.db import API_WORKERS
from app.modules.catalog.images.pipeline import ImagePipeline
from app.modules.kb.files import FilePipeline
from app.modules.kb.files.scanner import ClamAV


def test_release_supervisor_uses_importable_factory_and_all_workers() -> None:
    root = Path(__file__).resolve().parents[2]
    fixture = yaml.safe_load((root / "compose.release-fixture.yml").read_text())
    command = fixture["services"]["api"]["command"]
    assert command == [
        "python",
        "/opt/release/browser_media_server.py",
        "--release-api",
        str(API_WORKERS),
    ]


def reject_application_import(
    monkeypatch: pytest.MonkeyPatch, *server_dependencies: str
) -> None:
    original = builtins.__import__

    def guarded(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name in ("app.main", *server_dependencies):
            raise AssertionError(f"Non-server composition imported {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


def test_importing_scanner_seam_does_not_initialize_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "path", sys.path.copy())
    reject_application_import(monkeypatch, "fastapi", "uvicorn")
    module = runpy.run_path(str(Path(__file__).with_name("browser_media_server.py")))
    assert callable(module["release_scanner"])


def test_release_launcher_preserves_uvicorn_supervision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uvicorn import _subprocess

    context = Mock()
    original = _subprocess.spawn

    def launch(application: str, **options: object) -> None:
        assert _subprocess.spawn is context
        assert application == "browser_media_server:create_release_app"
        assert options == {
            "factory": True,
            "workers": API_WORKERS,
            "host": "0.0.0.0",
            "port": 8000,
            "access_log": False,
            "proxy_headers": False,
        }

    choose_context = Mock(return_value=context)
    reject_application_import(monkeypatch)
    monkeypatch.setattr("multiprocessing.get_context", choose_context)
    monkeypatch.setattr(uvicorn, "run", launch)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(
        sys, "argv", ["browser_media_server.py", "--release-api", str(API_WORKERS)]
    )
    with pytest.raises(SystemExit):
        runpy.run_path(
            str(Path(__file__).with_name("browser_media_server.py")),
            run_name="__main__",
        )
    choose_context.assert_called_once_with("forkserver")
    context.set_forkserver_preload.assert_called_once_with(["release_api_preload"])
    assert _subprocess.spawn is original


@pytest.mark.parametrize("documents", [False, True])
def test_fixture_launches_importable_factory_with_all_api_workers(
    monkeypatch: pytest.MonkeyPatch, documents: bool
) -> None:
    launch = Mock()
    reject_application_import(monkeypatch)
    monkeypatch.setattr("uvicorn.run", launch)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(
        sys,
        "argv",
        ["browser_media_server.py"] + (["--documents"] if documents else []),
    )
    runpy.run_path(
        str(Path(__file__).with_name("browser_media_server.py")), run_name="__main__"
    )
    expected = "create_document_app" if documents else "create_image_app"
    launch.assert_called_once_with(
        "browser_media_server:" + expected,
        factory=True,
        workers=API_WORKERS,
        host="0.0.0.0",
        port=8000,
        access_log=False,
        proxy_headers=False,
    )


def test_scanner_composition_lives_until_original_lifespan_finishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setenv("APP_FILES_DIR", str(tmp_path))
    module = runpy.run_path(str(Path(__file__).with_name("browser_media_server.py")))
    events = []

    @asynccontextmanager
    async def original(application: FastAPI) -> AsyncIterator[None]:
        events.append("start")
        yield
        assert signatures.exists()
        events.append("stop")

    images_original = ImagePipeline.from_environment
    documents_original = FilePipeline.from_environment
    application = module["_synthetic_app"](FastAPI(lifespan=original), documents=True)
    with TestClient(application):
        assert events == ["start"]
        images = ImagePipeline.from_environment()
        documents = FilePipeline.from_environment()
        assert isinstance(images.scanner, ClamAV)
        assert isinstance(documents.scanner, ClamAV)
        signatures = images.scanner.database
        assert signatures == documents.scanner.database
        assert images.scanner._database()[0] == "synthetic-browser-only"
        assert documents.scanner._database()[0] == "synthetic-browser-only"
    assert events == ["start", "stop"] and not signatures.exists()
    assert ImagePipeline.from_environment == images_original
    assert FilePipeline.from_environment == documents_original
