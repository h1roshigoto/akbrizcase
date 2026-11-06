"""Controller contracts; live Docker/PG/TLS proof is release_rehearsal.py."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from release import Blocked, Release, SCHEMA, fingerprint, manifest_valid, write_json


def manifest():
    return {
        "format": 1,
        "commit": "a" * 40,
        "schema": SCHEMA,
        "backend": "sha256:" + "b" * 64,
        "frontend": "sha256:" + "c" * 64,
        "postgres": "postgres@sha256:" + "d" * 64,
    }


class Harness(Release):
    def __init__(self, root, failure=None):
        super().__init__(root, manifest())
        self.calls = []
        self.failure = failure

    def event(self, stage):
        super().event(stage)
        self.calls.append(stage)

    def images(self):
        if self.failure == "IMAGES":
            raise Blocked

    def compose(self, *args, **kwargs):
        if self.stage == self.failure:
            raise Blocked
        return ""

    def sql(self, query):
        return SCHEMA if "alembic_version" in query else "akbrizcase"

    def smoke(self):
        if self.failure == "SMOKE":
            raise Blocked


class ReleaseTest(unittest.TestCase):
    def test_identity_rejects_mutable_missing_and_mismatched(self):
        manifest_valid(manifest())
        for change in (
            {"backend": "app:mvp-003"},
            {"frontend": "app:latest"},
            {"postgres": "postgres:16"},
            {"schema": "old"},
            {"commit": "main"},
            {"backend": "sha256:" + "0" * 63},
        ):
            with self.subTest(change=change), self.assertRaises(Blocked):
                manifest_valid(manifest() | change)
        with self.assertRaises(Blocked):
            manifest_valid({})

    def test_required_stage_failure_never_records_acceptance(self):
        for stage in ("IMAGES", "DATABASE", "MIGRATION", "READINESS", "START", "SMOKE"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                release = Harness(root, stage)
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaises(Blocked),
                ):
                    release.apply()
                self.assertFalse((root / "accepted.json").exists())
                self.assertFalse((root / "status.json").exists())
                if stage in {"IMAGES", "DATABASE", "MIGRATION", "READINESS"}:
                    self.assertNotIn("START", release.calls)

    def test_sequence_and_compatible_rollback_never_migrates(self):
        with (
            tempfile.TemporaryDirectory() as name,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            root = Path(name)
            first = Harness(root)
            first.apply()
            self.assertEqual(
                first.calls,
                [
                    "IMAGES",
                    "DATABASE",
                    "QUIESCE",
                    "MIGRATION",
                    "READINESS",
                    "START",
                    "SMOKE",
                    "ACCEPTED_LOCAL_REHEARSAL",
                ],
            )
            self.assertEqual(
                json.loads((root / "status.json").read_text())["real_staging"],
                "BLOCKED",
            )
            rollback = Harness(root)
            rollback.apply(rollback=True)
            self.assertNotIn("MIGRATION", rollback.calls)
            self.assertIn("ROLLBACK_COMPATIBILITY", rollback.calls)

    def test_unconfirmed_rollback_requires_manual_gate(self):
        with (
            tempfile.TemporaryDirectory() as name,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            root = Path(name)
            release = Harness(root)
            with self.assertRaises(Blocked):
                release.apply(rollback=True)
            self.assertEqual(release.calls, [])
            write_json(
                root / "accepted.json",
                {
                    "releases": [
                        {"manifest": manifest(), "fingerprint": fingerprint(manifest())}
                    ]
                },
            )
            with patch.object(release, "sql", return_value="incompatible"):
                with self.assertRaises(Blocked):
                    release.apply(rollback=True)
            self.assertNotIn("START", release.calls)
            self.assertNotIn("MIGRATION", release.calls)


if __name__ == "__main__":
    unittest.main()
