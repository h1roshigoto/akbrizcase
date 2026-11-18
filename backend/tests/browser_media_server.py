"""Synthetic-only composition for the guarded HTTPS catalog browser test.

Mounted only by scripts/https_smoke.py's catalog acceptance; never copied into
runtime images. Real clamscan and the production Pillow sandbox remain in use.
No HTTP parameter or production environment setting can select this scanner.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from multiprocessing import get_context, get_start_method
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from fastapi import FastAPI

# Executed from a read-only test bind at /tmp, outside the application package.
sys.path.insert(0, "/app")

# These imports prepare route definitions, without constructing app.main or running
# a lifespan. The forkserver shares those pages and never accepts API traffic.
RELEASE_PRELOAD = (
    "app.admin.auth",
    "app.admin.orders",
    "app.admin.sqladmin",
    "app.modules.catalog.router",
    "app.modules.chat.router",
    "app.modules.kb.router",
    "app.modules.orders.router",
)


@contextmanager
def release_scanner() -> Iterator[None]:
    from app.core.release import FullReleaseModes, release_modes, require_files
    from app.modules.kb.files.scanner import ClamAV

    # Only the exact-source, controller-selected test bind can select this seam.
    assert isinstance(release_modes(), FullReleaseModes)
    require_files()
    database = Path("/var/lib/akbrizcase/clamav")

    def checked_database(self: ClamAV) -> tuple[str, int]:
        signature = database / "test.ndb"
        if (
            database.is_symlink()
            or set(p.name for p in database.iterdir()) != {"test.ndb"}
            or signature.is_symlink()
            or signature.stat().st_mode & 0o777 != 0o444
            or signature.read_bytes()
            != b"Synthetic.Browser.Test:0:*:53594e5448494e464543544544\n"
        ):
            from app.modules.kb.files.process import FileRejected

            raise FileRejected("SIGNATURES_INVALID")
        epoch = int(signature.stat().st_mtime)
        self.require_fresh(epoch)
        return "synthetic-release-only", epoch

    with patch.object(ClamAV, "_database", checked_database):
        original_init = ClamAV.__init__
        with patch.object(
            ClamAV, "__init__", lambda self: original_init(self, database)
        ):
            yield


def _synthetic_app(application: FastAPI, *, documents: bool) -> FastAPI:
    from app.core.release import FullReleaseModes, release_modes

    original_lifespan = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # The factory runs separately in every worker. Keep signatures and
        # dependency composition alive for that worker's complete lifespan.
        if isinstance(release_modes(), FullReleaseModes):
            with release_scanner():
                async with original_lifespan(application):
                    yield
            return
        from app.modules.catalog.images.pipeline import ImagePipeline
        from app.modules.kb.files import FilePipeline
        from app.modules.kb.files.scanner import ClamAV

        class SyntheticClamAV(ClamAV):
            def _database(self) -> tuple[str, int]:
                epoch = int((self.database / "test.ndb").stat().st_mtime)
                self.require_fresh(epoch)
                return "synthetic-browser-only", epoch

        with TemporaryDirectory(prefix="synthetic-browser-av-") as temporary:
            signatures = Path(temporary)
            (signatures / "test.ndb").write_text(
                "Synthetic.Browser.Test:0:*:53594e5448494e464543544544\n"
            )
            pipeline = ImagePipeline(
                Path(os.environ["APP_FILES_DIR"]), _scanner=SyntheticClamAV(signatures)
            )
            with ExitStack() as composition:
                composition.enter_context(
                    patch.object(
                        ImagePipeline, "from_environment", return_value=pipeline
                    )
                )
                if documents:
                    document_pipeline = FilePipeline(
                        Path(os.environ["APP_FILES_DIR"]),
                        _scanner=SyntheticClamAV(signatures),
                    )
                    composition.enter_context(
                        patch.object(
                            FilePipeline,
                            "from_environment",
                            return_value=document_pipeline,
                        )
                    )
                async with original_lifespan(application):
                    yield

    application.router.lifespan_context = lifespan
    return application


def create_image_app() -> FastAPI:
    from app.main import app

    return _synthetic_app(app, documents=False)


def create_document_app() -> FastAPI:
    from app.main import app

    return _synthetic_app(app, documents=True)


def create_release_app() -> FastAPI:
    assert get_start_method() == "forkserver"
    assert "app.main" not in sys.modules
    assert all(name in sys.modules for name in RELEASE_PRELOAD)
    return create_document_app()


def serve(factory: str, workers: int) -> None:
    import uvicorn

    uvicorn.run(
        "browser_media_server:" + factory,
        factory=True,
        workers=workers,
        host="0.0.0.0",
        port=8000,
        access_log=False,
        proxy_headers=False,
    )


def serve_release(workers: int) -> None:
    from uvicorn import _subprocess

    context = get_context("forkserver")
    context.set_forkserver_preload(list(RELEASE_PRELOAD))
    # Uvicorn's pinned supervisor still manages its sockets, workers, readiness
    # and shutdown. Only the child creation context changes in this fixture.
    with patch.object(_subprocess, "spawn", context):
        serve("create_release_app", workers)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--release-kb"]:
        from app.modules.kb.cli import main as kb_main

        # Explicit test-only composition for the ordinary protected KB CLI.
        # Its hidden input, authentication, role and lifecycle checks stay intact.
        with release_scanner():
            raise SystemExit(kb_main(sys.argv[2:]))
    if sys.argv[1:2] == ["--release-probe"]:
        from app.core.release_probe import main

        with release_scanner():
            raise SystemExit(main())
    if sys.argv[1:2] == ["--release-api"]:
        serve_release(int(sys.argv[2]))
        raise SystemExit
    assert sys.argv[1:] in ([], ["--documents"])
    from app.core.db import API_WORKERS

    factory = "create_document_app" if sys.argv[1:] else "create_image_app"
    serve(factory, API_WORKERS)
