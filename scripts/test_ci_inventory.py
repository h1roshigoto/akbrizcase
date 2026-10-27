"""Inventory artifacts retain outcomes while excluding captured runtime output."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ci_inventory import record_verbose


class ArtifactTests(unittest.TestCase):
    def test_artifact_contains_only_ids_outcomes_and_duration(self):
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.dict(os.environ, CI_INVENTORY_DIR=temporary),
                redirect_stdout(io.StringIO()),
            ):
                record_verbose(
                    "db-core",
                    "private-stdout-marker\ntests/a.py::one PASSED [50%]\ntests/a.py::two SKIPPED [100%]\n=== 1 passed, 1 skipped in 2.5s ===",
                )
            text = (Path(temporary) / "db-core.json").read_text()
            self.assertNotIn("private-stdout-marker", text)
            self.assertEqual(
                json.loads(text),
                {
                    "suite": "db-core",
                    "tests": [
                        ["tests/a.py::one", "PASSED"],
                        ["tests/a.py::two", "SKIPPED"],
                    ],
                    "pytest_seconds": 2.5,
                },
            )

    def test_missing_verbose_outcome_fails_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.dict(os.environ, CI_INVENTORY_DIR=temporary),
                self.assertRaisesRegex(ValueError, "outcomes"),
            ):
                record_verbose(
                    "db-core",
                    "tests/a.py::one PASSED [50%]\n=== 2 passed in 1.0s ===",
                )
            self.assertEqual(list(Path(temporary).iterdir()), [])
