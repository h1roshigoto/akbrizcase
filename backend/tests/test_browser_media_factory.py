"""The owned scanner fixture uses the same worker count and full lifespan."""

import runpy
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.db import API_WORKERS
from app.modules.catalog.images.pipeline import ImagePipeline
from app.modules.kb.files import FilePipeline


@pytest.mark.parametrize("documents", [False, True])
def test_fixture_launches_importable_factory_with_all_api_workers(
    monkeypatch: pytest.MonkeyPatch, documents: bool
) -> None:
    launch = Mock()
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
        assert isinstance(images.scanner, module["SyntheticClamAV"])
        assert isinstance(documents.scanner, module["SyntheticClamAV"])
        signatures = images.scanner.database
        assert signatures == documents.scanner.database
        assert images.scanner._database()[0] == "synthetic-browser-only"
    assert events == ["start", "stop"] and not signatures.exists()
    assert ImagePipeline.from_environment == images_original
    assert FilePipeline.from_environment == documents_original
