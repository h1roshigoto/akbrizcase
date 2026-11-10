"""Independent evidence, timing, safety and runner failure guards."""

import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from mvp048_result import (
    MIN_SAMPLES,
    OPERATIONS,
    PROFILES,
    Profile,
    check_sample,
    make_result,
    overlap,
    percentile,
    publish,
    statistics,
    validate_result,
)
from mvp048_smoke import Resources, receive, target_preflight

HEAD = "a" * 40


def sample(
    start=1000000, duration=1000000, operation="button", phase="measurement", **extra
):
    return {
        "phase": phase,
        "operation": operation,
        "status": 200 if operation != "order" else 201,
        "ok": True,
        "timeout": False,
        "start_ns": start,
        "end_ns": start + duration,
        "client": 0,
        **extra,
    }


def evidence():
    samples = [
        sample(start=1_000_000_000 + i * 1_000_000, operation=op, client=i % 200)
        for op in OPERATIONS
        for i in range(200)
    ]
    resource = Resources(None, None, None).report()
    result = make_result(
        head=HEAD,
        dirty=False,
        profile=PROFILES["pilot200-v1"],
        samples=samples,
        complete=True,
        identities={
            **{
                key: "sha256:" + "b" * 64
                for key in ("backend", "frontend", "postgres", "generator")
            },
            "schema": "0011_mvp039_http_security",
        },
        corpus={"items": 54, "documents": 1, "versions": 1, "sections": 1, "trees": 1},
        resources=resource,
        outcomes={
            key: "PASS" if key != "backup" else "BLOCKED"
            for key in (
                "invariants",
                "https",
                "contracts",
                "rate_limits",
                "smtp",
                "lease",
                "database",
                "backup",
            )
        },
        source_ip_count=30,
    )
    return result, samples


class Measurements(unittest.TestCase):
    def test_nearest_rank_is_not_interpolation(self):
        self.assertEqual(percentile(list(range(1, 21)), 0.95), 19)
        self.assertEqual(percentile([300, 1, 2], 0.95), 300)
        self.assertIsNone(percentile([], 0.95))

    def test_real_overlap_differs_from_created_tasks(self):
        self.assertEqual(
            overlap([sample(i * 1_000_000 + 1) for i in range(30)])["peak_active"], 1
        )
        self.assertEqual(overlap([sample(1) for i in range(30)])["peak_active"], 30)
        self.assertEqual(overlap([sample(1), sample(500001)])["mean_active"], 4 / 3)

    def test_warmup_and_errors_are_never_removed_silently(self):
        samples = [sample(phase="warmup", duration=8000000000)]
        samples += [sample() for _ in range(20)]
        stats = statistics(samples, "button", 10)
        self.assertEqual(stats["success_ms"]["p95"], 1)
        self.assertEqual(stats["successes"], 20)
        samples.append(sample(status=500, ok=False, duration=9000000000))
        stats = statistics(samples, "button", 10)
        self.assertEqual(stats["gate"], "FAIL")
        self.assertEqual(stats["statuses"], {"200": 20, "500": 1})
        self.assertEqual(stats["all_attempt_ms"]["p99"], 9000)
        self.assertEqual(stats["success_ms"]["maximum"], 1)

    def test_small_sample_or_timeout_cannot_pass(self):
        self.assertEqual(
            statistics([sample()] * (MIN_SAMPLES - 1), "button", 10)["gate"],
            "INCONCLUSIVE",
        )
        for status, timeout in ((0, True), (429, False), (422, False), (503, False)):
            stats = statistics(
                [sample()] * 20 + [sample(status=status, timeout=timeout, ok=False)],
                "button",
                10,
            )
            self.assertEqual(stats["gate"], "FAIL")
            self.assertEqual(stats["errors"], 1)
            self.assertEqual(stats["timeouts"], int(timeout))

    def test_slow_correct_response_fails_threshold(self):
        stats = statistics([sample(duration=301000000)] * 20, "button", 10)
        self.assertEqual(stats["gate"], "FAIL")
        self.assertEqual(stats["all_attempt_ms"]["maximum"], 301)

    def test_profile_limits_and_fixed_digest(self):
        self.assertEqual(PROFILES["pilot200-v1"].clients, 200)
        self.assertEqual(PROFILES["pilot200-v1"].concurrency, 30)
        for key, value in [
            ("clients", 201),
            ("sources", 31),
            ("concurrency", 31),
            ("measurement_seconds", 121),
            ("orders_per_source", 6),
            ("total_timeout", 1201),
            ("request_timeout", 0),
            ("clients", True),
        ]:
            data = asdict(PROFILES["pilot200-v1"])
            data[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                Profile(**data)

    def test_sample_types_and_ranges(self):
        for extra in (
            {"status": True},
            {"client": 200},
            {"timeout": 1},
            {"ok": True, "status": 429},
            {"timeout": True, "status": 200},
            {"end_ns": 0},
            {"token": "secret"},
        ):
            with self.subTest(extra=list(extra)), self.assertRaises(ValueError):
                check_sample(sample(**extra))


class Evidence(unittest.TestCase):
    def test_closed_complete_synthetic_is_never_target_admission(self):
        result, _ = evidence()
        self.assertEqual(result["gates"]["SYNTHETIC_SMOKE"], "PASS")
        self.assertEqual(result["gates"]["TARGET_LOAD"], "BLOCKED")
        self.assertEqual(result["gates"]["PRODUCTION_ADMISSION"], "BLOCKED")
        self.assertEqual(
            result["resources"]["metrics"]["pg_connections"]["status"], "unavailable"
        )

    def test_stale_head(self):
        result, _ = evidence()
        with self.assertRaises(ValueError):
            validate_result(result, expected_head="c" * 40)

    def test_profile_tampering_even_with_new_digest(self):
        result, _ = evidence()
        result["profile"]["concurrency"] = 1
        with self.assertRaises(ValueError):
            validate_result(result, expected_head=HEAD)

    def test_inconsistent_totals_codes_percentiles_and_gates(self):
        for mutation in (
            lambda r: r["operations"]["order"].update(successes=201),
            lambda r: r["operations"]["order"]["statuses"].update({"201": 199}),
            lambda r: r["operations"]["order"].update(attempt_rps=0),
            lambda r: r["operations"]["order"]["success_ms"].update(p50=100),
            lambda r: r["operations"]["order"].update(gate="BLOCKED"),
            lambda r: r["gates"].update(TARGET_LOAD="PASS"),
            lambda r: r["phases"].update(measurement=1),
            lambda r: r["activity"].update(peak_active=31),
            lambda r: r["network"].update(source_ip_count=1),
            lambda r: r["resources"]["metrics"]["pg_connections"].update(last=0),
            lambda r: r["corpus"].update(items=101),
        ):
            result, _ = evidence()
            mutation(result)
            with self.assertRaises(ValueError):
                validate_result(result, expected_head=HEAD)

    def test_sensitive_and_arbitrary_fields_rejected_at_every_level(self):
        for keys in (
            (),
            ("operations", "order"),
            ("resources",),
            ("resources", "metrics"),
            ("identities",),
            ("outcomes",),
            ("network",),
            ("corpus",),
        ):
            result, _ = evidence()
            node = result
            for key in keys:
                node = node[key]
            node["contact"] = "canary@example.invalid"
            with self.assertRaises(ValueError):
                validate_result(result, expected_head=HEAD)

    def test_publication_checks_private_ledger_before_writing(self):
        result, samples = evidence()
        samples[-1]["end_ns"] += 2000000000
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                publish(result, samples, directory, HEAD)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_safe_publication_private_mode_no_overwrite(self):
        result, samples = evidence()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            publish(result, samples, directory, HEAD)
            for path in Path(directory).iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            text = output.getvalue()
            for forbidden in (
                "@",
                "X-Chat-Token",
                "cookie",
                "SourceRef",
                "http://",
                "https://",
                "/tmp/",
            ):
                self.assertNotIn(forbidden, text)
            with self.assertRaises(FileExistsError):
                publish(result, samples, directory, HEAD)

    def test_incomplete_recovery_never_passes(self):
        result, _ = evidence()
        result["complete"] = False
        result["gates"]["SYNTHETIC_SMOKE"] = "INCONCLUSIVE"
        result["outcomes"]["smtp"] = "NOT RUN"
        result["gates"]["OPERATIONAL_RECOVERY"] = "BLOCKED"
        validate_result(result, expected_head=HEAD)
        result["gates"]["SYNTHETIC_SMOKE"] = "PASS"
        with self.assertRaises(ValueError):
            validate_result(result, expected_head=HEAD)


class Runner(unittest.TestCase):
    def test_target_without_machine_or_adapter_blocks_without_io(self):
        with patch("subprocess.run", side_effect=AssertionError("must not run")):
            self.assertEqual(target_preflight(None)["TARGET_LOAD"], "BLOCKED")
            self.assertEqual(
                target_preflight(Path("/not/a/target"))["TARGET_LOAD"], "BLOCKED"
            )

    def test_receive_timeout_and_incomplete_child(self):
        for code in ("import time; time.sleep(1)", "pass"):
            process = subprocess.Popen(
                [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
            )
            try:
                with self.assertRaises((TimeoutError, ValueError)):
                    receive([process], "ready", time.monotonic() + 0.05)
            finally:
                process.kill()
                process.wait(timeout=3)
                process.stdout.close()

    def test_cancelled_child_and_wrong_phase_are_not_success(self):
        for payload in (
            {"stage": "done", "source": 0},
            {"stage": "failed"},
            {"stage": "ready", "source": 1},
        ):
            code = "import json; print(" + repr(json.dumps(payload)) + ", flush=True)"
            process = subprocess.Popen(
                [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
            )
            try:
                with self.assertRaises(ValueError):
                    receive([process], "ready", time.monotonic() + 3)
            finally:
                process.wait(timeout=3)
                process.stdout.close()

    def test_empty_resource_sources_are_unavailable_never_zero(self):
        resources = Resources(None, None, None).report()
        self.assertEqual(resources["samples"], 0)
        for metric in resources["metrics"].values():
            self.assertEqual(
                metric,
                {
                    "status": "unavailable",
                    "minimum": None,
                    "maximum": None,
                    "last": None,
                },
            )


class TargetPreflight(unittest.TestCase):
    def packet(self):
        from datetime import UTC, datetime, timedelta

        return {
            "schema": "MVP048_AUTHORIZATION/v1",
            "head": HEAD,
            "profile_digest": PROFILES["pilot200-v1"].digest,
            "host_identity_sha256": "c" * 64,
            "operator_reference_sha256": "d" * 64,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            "project": "akbrizcase-https-test-0123456789",
            "images": {
                key: "sha256:" + "b" * 64
                for key in ("backend", "frontend", "postgres", "generator")
            },
            "origin": "https://localhost:8443",
            "ca_file": "/protected/ca.crt",
            "dataset_sha256": "e" * 64,
            "synthetic_only": True,
            "ssd_attested": True,
            "backup_receipt": "/protected/backup.json",
            "runtime_journal": "/protected/runtime.jsonl",
        }

    def check(self, packet):
        from datetime import UTC, datetime

        from mvp048_target import checked_packet

        return checked_packet(
            packet, head=HEAD, host_digest="c" * 64, now=datetime.now(UTC)
        )

    def test_exact_bound_packet_is_eligible_for_live_preflight_only(self):
        self.assertEqual(self.check(self.packet())["head"], HEAD)

    def test_target_preflight_rejects_all_unbound_inputs(self):
        mutations = [
            ("head", "b" * 40),
            ("host_identity_sha256", "a" * 64),
            ("profile_digest", "f" * 64),
            ("synthetic_only", False),
            ("synthetic_only", 1),
            ("expires_at", "2000-01-01T00:00:00+00:00"),
            ("expires_at", "2099-01-01T00:00:00+00:00"),
            ("origin", "http://localhost:8443"),
            ("origin", "https://example.com:8443"),
            ("origin", "https://localhost:8443/?token=secret"),
            ("origin", "https://user:secret@localhost:8443"),
            ("project", "production"),
            ("ca_file", "relative.crt"),
            ("operator_reference_sha256", "invented"),
            ("ssd_attested", False),
            ("runtime_journal", None),
            ("backup_receipt", "relative.json"),
        ]
        for key, value in mutations:
            packet = self.packet()
            packet[key] = value
            with self.subTest(field=key), self.assertRaises((ValueError, TypeError)):
                self.check(packet)

    def test_missing_fields_and_unknown_personal_fields_rejected(self):
        for key in self.packet():
            packet = self.packet()
            del packet[key]
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.check(packet)
        packet = self.packet()
        packet["contact"] = "canary@example.invalid"
        with self.assertRaises(ValueError):
            self.check(packet)

    def test_absent_authorization_or_resource_cannot_promote_target(self):
        result, _ = evidence()
        result["environment"] = "authorized_target"
        with self.assertRaises(ValueError):
            validate_result(result, expected_head=HEAD)
        result["authorization_digest"] = "f" * 64
        validate_result(result, expected_head=HEAD)
        self.assertEqual(result["gates"]["TARGET_LOAD"], "BLOCKED")
        result["gates"]["TARGET_LOAD"] = "PASS"
        with self.assertRaises(ValueError):
            validate_result(result, expected_head=HEAD)

    def test_dirty_source_cannot_have_final_smoke_pass(self):
        result, _ = evidence()
        result["dirty"] = True
        with self.assertRaises(ValueError):
            validate_result(result, expected_head=HEAD)

    def test_error_attempt_prevents_smoke_pass(self):
        from mvp048_result import derive_gates

        result, _ = evidence()
        result["operations"]["knowledge"]["errors"] = 1
        self.assertEqual(derive_gates(result)["SYNTHETIC_SMOKE"], "FAIL")


class Cleanup(unittest.TestCase):
    def test_cancel_cleanup_stops_every_owned_source_and_preserves_volumes(self):
        from unittest.mock import Mock

        from mvp048_smoke import cleanup_sources

        stack = Mock(project="project")

        def docker(*args):
            if args[0] == "inspect":
                return json.dumps(
                    [{"Config": {"Labels": {"com.akbrizcase.load-owner": "project"}}}]
                )
            return ""

        stack.docker.side_effect = docker
        process = Mock(
            stdin=io.StringIO(),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            returncode=0,
        )
        self.assertTrue(cleanup_sources(stack, ["a", "b"], [process]))
        calls = [call.args for call in stack.docker.call_args_list]
        self.assertEqual([v for v in calls if v[0] == "rm"], [("rm", "a"), ("rm", "b")])
        self.assertFalse(any("volume" in call or "--volumes" in call for call in calls))
        self.assertTrue(
            process.stdin.closed and process.stdout.closed and process.stderr.closed
        )

    def test_foreign_source_is_preserved_but_other_owned_sources_are_cleaned(self):
        from unittest.mock import Mock

        from mvp048_smoke import cleanup_sources

        stack = Mock(project="project")

        def docker(*args):
            if args[0] == "inspect":
                owner = "foreign" if args[1] == "a" else "project"
                return json.dumps(
                    [{"Config": {"Labels": {"com.akbrizcase.load-owner": owner}}}]
                )
            return ""

        stack.docker.side_effect = docker
        self.assertFalse(cleanup_sources(stack, ["a", "b"], []))
        calls = [call.args for call in stack.docker.call_args_list]
        self.assertNotIn(("rm", "a"), calls)
        self.assertIn(("rm", "b"), calls)

    def test_failed_child_cannot_have_successful_cleanup_outcome(self):
        from unittest.mock import Mock

        from mvp048_smoke import cleanup_sources

        stack = Mock(project="project")
        process = Mock(
            stdin=io.StringIO(),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            returncode=1,
        )
        self.assertFalse(cleanup_sources(stack, [], [process]))


if __name__ == "__main__":
    unittest.main()
