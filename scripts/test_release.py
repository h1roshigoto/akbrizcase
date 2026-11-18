"""Controller contracts; live Docker/PG/TLS proof is release_rehearsal.py."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from release import (
    FULL_MODES,
    MODES,
    SCHEMA,
    Blocked,
    Release,
    fingerprint,
    manifest_valid,
    protected,
    write_json,
)
from release_resource_probe import (
    ResourceProbe,
    forkserver_command,
    require_resource_bounds,
    role_memory,
)


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
        self.config = {
            "project": "akbriz-staging-0123456789ab",
            "av": "synthetic-fixture",
        }
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
    def test_private_runtime_files_cannot_have_external_hardlinks(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "synthetic-secret"
            path.write_text("SYNTHETIC ONLY")
            path.chmod(0o600)
            protected(path)
            alias = Path(name) / "alias"
            os.link(path, alias)
            for candidate in (path, alias):
                with self.assertRaises(Blocked):
                    protected(candidate)

    def test_receipts_bind_profile_and_modes_and_preserve_legacy(self):
        release = Harness(Path("/tmp/unused-release-contract"))
        legacy = {"manifest": manifest(), "fingerprint": fingerprint(manifest())}
        self.assertTrue(release.accepted({"releases": [legacy]}))
        reduced_receipt = release.receipt()
        release.full = True
        release.modes = FULL_MODES.copy()
        self.assertFalse(release.accepted({"releases": [legacy, reduced_receipt]}))
        full_receipt = release.receipt()
        self.assertFalse(
            release.accepted(
                {
                    "releases": [
                        full_receipt | {"project": "akbriz-staging-ffffffffffff"}
                    ]
                }
            )
        )
        self.assertFalse(
            release.accepted({"releases": [full_receipt | {"av": "signed-cvd"}]})
        )
        self.assertTrue(release.accepted({"releases": [full_receipt]}))
        for change in (
            {"profile": "production"},
            {"modes": fingerprint(MODES)},
            {"format": True},
            {"fingerprint": "0" * 64},
        ):
            with self.subTest(change=change), self.assertRaises(Blocked):
                release.accepted({"releases": [full_receipt | change]})
        # A valid first receipt cannot hide a malformed later record.
        with self.assertRaises(Blocked):
            release.accepted(
                {"releases": [full_receipt, full_receipt | {"extra": True}]}
            )

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


class ResourceEvidenceTest(unittest.TestCase):
    def test_forkserver_detection_allows_interpreter_flags(self):
        command = b"from multiprocessing.forkserver import main; main(1, 2)"
        self.assertTrue(forkserver_command([b"python", b"-B", b"-c", command]))
        self.assertFalse(forkserver_command([b"python", b"-c", b"# " + command]))

    def evidence(self):
        return {
            "memory.max": 512 * 1024**2,
            "memory.current": 300 * 1024**2,
            "memory.peak": 450 * 1024**2,
            "memory.swap.current": 0,
            "memory.swap.max": 512 * 1024**2,
            "memory.swap.peak": 0,
            "memory.events": {"max": 0, "oom": 0, "oom_kill": 0},
            "pids.max": 64,
            "pids.current": 16,
            "pids.peak": 40,
            "pids.events": {"max": 0},
            "cpu.max": (100000, 100000),
        }

    def test_unchanged_envelope_and_closed_sampler_cleanup(self):
        evidence = self.evidence()
        require_resource_bounds(evidence)
        with (
            patch("release_resource_probe.resource_snapshot", return_value=evidence),
            patch("release_resource_probe.role_memory", return_value={}),
        ):
            probe = ResourceProbe()
            probe.start()
            try:
                raise RuntimeError("synthetic operation refusal")
            except RuntimeError:
                pass
            finally:
                result = probe.finish()
            self.assertFalse(probe.thread.is_alive())
            self.assertGreaterEqual(result["samples"], 2)

    def test_pressure_oom_pid_swap_and_widened_bounds_refuse(self):
        changes = (
            {"memory.events": {"max": 1, "oom": 0, "oom_kill": 0}},
            {"memory.events": {"max": 0, "oom": 1, "oom_kill": 0}},
            {"memory.events": {"max": 0, "oom": 0, "oom_kill": 1}},
            {"memory.events": {"max": 0, "oom": 0, "oom_kill": 0, "oom_group_kill": 1}},
            {"pids.events": {"max": 1}},
            {"memory.max": 1024 * 1024**2},
            {"memory.peak": 512 * 1024**2},
            {"memory.peak": 513 * 1024**2},
            {"memory.peak": None},
            {"memory.swap.current": 4096},
            {"memory.swap.peak": 4096},
            {"memory.swap.peak": None},
            {"pids.max": 65},
            {"pids.peak": 65},
            {"cpu.max": (200000, 100000)},
        )
        for change in changes:
            with (
                self.subTest(change=change),
                self.assertRaises((AssertionError, TypeError)),
            ):
                require_resource_bounds(deepcopy(self.evidence()) | change)

    def test_process_evidence_emits_only_roles_and_numeric_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            process = Path(directory) / "1"
            process.mkdir()
            (process / "cmdline").write_bytes(
                b"python\0PRIVATE_ARG\0/private/path\0--multiprocessing-fork\0"
            )
            (process / "comm").write_text("python")
            (process / "smaps_rollup").write_text(
                "Rss: 100 kB\nPss: 80 kB\nSwapPss: 0 kB\n"
            )
            with patch.object(Path, "glob", return_value=iter([process])):
                result = role_memory()
            self.assertEqual(
                result,
                {
                    "api_worker": {
                        "count": 1,
                        "rss": 102400,
                        "pss": 81920,
                        "swap_pss": 0,
                    }
                },
            )
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.assertNotIn("/private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
