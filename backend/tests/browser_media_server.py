"""Synthetic-only composition for the guarded HTTPS catalog browser test.

Mounted only by scripts/https_smoke.py's catalog acceptance; never copied into
runtime images. Real clamscan and the production Pillow sandbox remain in use.
No HTTP parameter or production environment setting can select this scanner.
"""

import os
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import uvicorn
from fastapi import FastAPI

# Executed from a read-only test bind at /tmp, outside the application package.
sys.path.insert(0, "/app")

from app.core.db import API_WORKERS  # noqa: E402
from app.core.release import FullReleaseModes, release_modes, require_files  # noqa: E402
from app.main import app  # noqa: E402
from app.modules.catalog.images.pipeline import ImagePipeline  # noqa: E402
from app.modules.kb.files import FilePipeline  # noqa: E402
from app.modules.kb.files.scanner import ClamAV  # noqa: E402


class SyntheticClamAV(ClamAV):
    def _database(self) -> tuple[str, int]:
        epoch = int((self.database / "test.ndb").stat().st_mtime)
        self.require_fresh(epoch)
        return "synthetic-browser-only", epoch


@contextmanager
def release_scanner() -> Iterator[None]:
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
    return _synthetic_app(app, documents=False)


def create_document_app() -> FastAPI:
    return _synthetic_app(app, documents=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--release-probe"]:
        from app.core.release_probe import main

        with release_scanner():
            raise SystemExit(main())
    assert sys.argv[1:] in ([], ["--documents"])
    factory = "create_document_app" if sys.argv[1:] else "create_image_app"
    uvicorn.run(
        "browser_media_server:" + factory,
        factory=True,
        workers=API_WORKERS,
        host="0.0.0.0",
        port=8000,
        access_log=False,
        proxy_headers=False,
    )
