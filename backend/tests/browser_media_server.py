"""Synthetic-only composition for the guarded HTTPS catalog browser test.

Mounted only by scripts/https_smoke.py's catalog acceptance; never copied into
runtime images. Real clamscan and the production Pillow sandbox remain in use.
No HTTP parameter or production environment setting can select this scanner.
"""

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import uvicorn

# Executed from a read-only test bind at /tmp, outside the application package.
sys.path.insert(0, "/app")

from app.main import app  # noqa: E402
from app.modules.catalog.images.pipeline import ImagePipeline  # noqa: E402
from app.modules.kb.files.scanner import ClamAV  # noqa: E402


class SyntheticClamAV(ClamAV):
    def _database(self) -> tuple[str, int]:
        epoch = int((self.database / "test.ndb").stat().st_mtime)
        self.require_fresh(epoch)
        return "synthetic-browser-only", epoch


if __name__ == "__main__":
    with TemporaryDirectory(prefix="synthetic-browser-av-") as temporary:
        signatures = Path(temporary)
        (signatures / "test.ndb").write_text(
            "Synthetic.Browser.Test:0:*:53594e5448494e464543544544\n"
        )
        pipeline = ImagePipeline(
            Path(os.environ["APP_FILES_DIR"]), _scanner=SyntheticClamAV(signatures)
        )
        with patch.object(ImagePipeline, "from_environment", return_value=pipeline):
            uvicorn.run(
                app, host="0.0.0.0", port=8000, access_log=False, proxy_headers=False
            )
