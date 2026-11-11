"""Fast offline guards: provenance, exclusions, confidentiality, no admission."""

import contextlib
import copy
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mvp050_readiness as gate
from mvp048_result import PROFILES, derive_gates, make_result
from mvp048_smoke import Resources
from mvp049_content_gate import Rejected, git, sha

CANARY = b"private-person@example.invalid /protected/secret token=PRIVATE-CANARY"


def opaque(n):
    return "ev-" + f"{n:032x}"


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mvp050-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git(
            "-c",
            "user.name=SYNTHETIC",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "SYNTHETIC ONLY",
            "--allow-empty",
        )
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.root = Path(self.temp.name) / "input"
        self.root.mkdir(mode=0o700)
        self.manifest = gate.baseline(self.head, self.head)
        self.records = {}

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True
        )

    def check(self, key):
        return next(c for c in self.manifest["checks"] if c["id"] == key)

    def add_evidence(self, key, scope="EXTERNAL_DECISION", data=CANARY, kind="opaque"):
        n = len(self.manifest["evidence"]) + 1
        eid, aid = opaque(2 * n), opaque(2 * n + 1)
        if not isinstance(data, bytes):
            data = gate.canonical(data)
        self.manifest["artifacts"].append(
            dict(id=aid, filename=f"artifact{n}.json", sha256=sha(data), kind=kind)
        )
        self.manifest["evidence"].append(
            dict(
                id=eid,
                requirement=key,
                scope=scope,
                binding_sha256=gate.digest(self.manifest["binding"]),
                artifact=aid,
            )
        )
        self.records[aid] = data
        if key in gate.CATALOG:
            self.check(key).update(status="PASS", evidence=eid)
        return eid

    def assess(self):
        return gate.assess(
            self.manifest,
            {a["id"]: a for a in self.manifest["artifacts"]},
            self.records,
        )

    def row(self, key):
        return next(r for r in self.assess()["checklist"] if r["id"] == key)

    def cli(self, **overrides):
        raw = gate.canonical(self.manifest)
        for artifact in (
            self.manifest["artifacts"]
            if type(self.manifest["artifacts"]) is list
            else []
        ):
            path = self.root / artifact["filename"]
            path.write_bytes(self.records[artifact["id"]])
            path.chmod(0o600)
        path = self.root / "checklist.json"
        path.write_bytes(raw)
        path.chmod(0o600)
        params = {
            "--repo": str(self.repo),
            "--input-root": str(self.root),
            "--expected-head": self.head,
            "--expected-base": self.head,
            "--expected-sha256": sha(raw),
            "--expected-binding-sha256": gate.digest(self.manifest["binding"]),
        }
        params.update(overrides)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = gate.main(["check", *(v for pair in params.items() for v in pair)])
        self.assertNotIn(CANARY.decode(), output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())
        return code, json.loads(output.getvalue().split("MVP050_RESULT ")[-1])

    def ci_snapshot(self):
        return dict(
            format="MVP050_CI_SNAPSHOT/v1",
            head=self.head,
            binding_sha256=gate.digest(self.manifest["binding"]),
            repository="h1roshigoto/akbrizcase",
            workflow=".github/workflows/ci.yml",
            event="push",
            branch="main",
            run_id=1,
            attempt=1,
            status="completed",
            conclusion="success",
            jobs=[
                dict(
                    name=n,
                    status="completed",
                    conclusion="success",
                    failures_masked=False,
                )
                for n in gate.JOBS
            ],
            inventories=[
                dict(
                    suite=s,
                    collected_sha256="b" * 64,
                    executed_sha256="b" * 64,
                    expected=1,
                    passed=1,
                    skipped=0,
                    failed=0,
                    xfailed=0,
                    errors=0,
                )
                for s in gate.SUITES
            ],
        )

    def add_ci(self, snapshot):
        self.add_evidence("A-CI", "CI_SYNTHETIC", snapshot, "ci")
        return self.row("A-CI")

    def reduced(self, feature="diagnose"):
        self.manifest["pilot"] = "REDUCED_PILOT"
        self.manifest["binding"]["enabled_features"].remove(feature)
        flag = {
            "diagnose": "chat",
            "orders": "orders",
            "knowledge": "knowledge",
            "files": "files",
            "catalog": "catalog",
        }.get(feature)
        if flag is not None:
            self.manifest["binding"]["modes"][flag] = False
        if feature == "smtp":
            self.manifest["binding"]["modes"]["smtp"] = "disabled"
        keys = [r["id"] for r in gate.REQUIREMENTS if r["feature"] == feature]
        self.manifest["scope"]["exclusions"] = [
            dict(
                feature=feature,
                disabled_endpoints=list(gate.ENDPOINTS[feature]),
                checks=keys,
            )
        ]
        self.manifest["scope"]["decision"] = self.add_evidence("SCOPE_DECISION")
        for key in keys:
            self.check(key)["status"] = "OUT_OF_SCOPE_APPROVED"
        return keys

    def load_report(self, target=False):
        samples = [
            dict(
                phase="measurement",
                operation=op,
                status=201 if op == "order" else 200,
                ok=True,
                timeout=False,
                start_ns=1_000_000_000 + (i // 30 + j * 7) * 2_000_000,
                end_ns=1_001_000_000 + (i // 30 + j * 7) * 2_000_000,
                client=i,
            )
            for j, op in enumerate(("button", "catalog", "knowledge", "order"))
            for i in range(200)
        ]
        resources = Resources(None, None, None).report()
        resources["metrics"]["runtime_smtp_failures"] = dict(
            status="observed", minimum=1, maximum=1, last=1
        )
        result = make_result(
            head=self.head,
            dirty=False,
            profile=PROFILES["pilot200-v1"],
            samples=samples,
            complete=True,
            identities=dict(
                **self.manifest["binding"]["images"],
                generator="sha256:" + "b" * 64,
                schema=gate.SCHEMA,
                application_revision=self.head,
                backend_source_tree="b" * 40,
                frontend_source_tree="c" * 40,
            ),
            corpus=dict(items=54, documents=1, versions=1, sections=1, trees=1),
            resources=resources,
            outcomes={
                k: "PASS"
                for k in (
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
        if target:
            result.update(
                environment="authorized_target", authorization_digest="c" * 64
            )
            resources.update(
                samples=2,
                runtime="observed",
                backup="observed",
                disk_medium="operator_attested_ssd",
            )
            resources["configuration"]["sqlalchemy_pool"].update(
                status="configured", pool_size=5, max_overflow=5, timeout_seconds=3
            )
            for name in resources["metrics"]:
                value = (
                    1_000_000_000
                    if name.endswith("disk_total_bytes")
                    else 500_000_000
                    if name.endswith("disk_free_bytes")
                    else 1
                )
                resources["metrics"][name] = dict(
                    status="observed", minimum=value, maximum=value, last=value
                )
            result["gates"] = derive_gates(result)
        return result

    def bind_complete(self):
        self.manifest["binding"]["images"] = {
            k: "sha256:" + "b" * 64 for k in ("backend", "frontend", "postgres")
        }
        self.manifest["binding"]["content_sha256"] = "d" * 64

    def assert_no_admission(self, result):
        for k in (
            "RELEASE_READINESS",
            "FULL_MVP_READINESS",
            "REDUCED_PILOT_READINESS",
            "PRODUCTION_ADMISSION",
        ):
            self.assertEqual(result[k], "BLOCKED")
        self.assertEqual(result["PUBLIC_LAUNCH_RECOMMENDATION"], "NO-GO")
        self.assertEqual(result["HUMAN_GO_NO_GO_DECISION"], "PENDING")
        self.assertFalse(any(r["status"] == "PASS" for r in result["checklist"]))

    def test_complete_closed_format_and_initial_no_go(self):
        code, result = self.cli()
        self.assertEqual(code, 0)
        self.assertEqual(result["TECHNICAL_READINESS_CHECK"], "PASS")
        self.assertEqual(len(result["checklist"]), 86)
        self.assert_no_admission(result)
        for row in result["checklist"]:
            self.assertTrue(
                {
                    "id",
                    "status",
                    "reason",
                    "owner",
                    "action",
                    "depends_on",
                    "blocks_public_launch",
                }
                <= row.keys()
            )

    def test_missing_p0_or_p1_rejected(self):
        for key in ("MVP-010", "MVP-054", "MVP-038", "C-TARGET-LOAD"):
            with self.subTest(key=key):
                old = self.manifest["checks"]
                self.manifest["checks"] = [c for c in old if c["id"] != key]
                self.assertEqual(self.cli()[0], 1)
                self.manifest["checks"] = old

    def test_unknown_fields_types_and_duplicate_checks_rejected(self):
        for change in (
            {"human_approved": True},
            {"signature": "approved"},
            {"pilot": "GO"},
            {"artifacts": False},
        ):
            with self.subTest(change=change):
                original = copy.deepcopy(self.manifest)
                self.manifest.update(change)
                self.assertEqual(self.cli()[0], 1)
                self.manifest = original
        self.manifest["checks"][0] = copy.deepcopy(self.manifest["checks"][1])
        self.assertEqual(self.cli()[0], 1)

    def test_expected_head_base_binding_and_packet_checksum(self):
        for flag, value in (
            ("--expected-head", "b" * 40),
            ("--expected-base", "b" * 40),
            ("--expected-sha256", "b" * 64),
            ("--expected-binding-sha256", "b" * 64),
        ):
            with self.subTest(flag=flag):
                self.assertEqual(self.cli(**{flag: value})[0], 1)

    def test_image_content_modes_release_id_and_schema_pins(self):
        original = copy.deepcopy(self.manifest)
        pin = gate.digest(original["binding"])
        for field, value in (
            (
                "images",
                dict.fromkeys(
                    ("backend", "frontend", "postgres"), "sha256:" + "b" * 64
                ),
            ),
            ("content_sha256", "b" * 64),
            ("schema", "old"),
            ("release_id", "rc-" + "1" * 32),
            ("enabled_features", ["catalog"]),
            ("modes", copy.deepcopy(gate.MODES)),
        ):
            with self.subTest(field=field):
                self.manifest = copy.deepcopy(original)
                self.manifest["binding"][field] = value
                self.assertEqual(self.cli(**{"--expected-binding-sha256": pin})[0], 1)

    def test_stale_and_wrong_requirement_evidence(self):
        self.add_evidence("E-POLICY")
        self.manifest["evidence"][0]["binding_sha256"] = "b" * 64
        self.assertEqual(self.cli()[0], 1)
        self.manifest["evidence"][0]["binding_sha256"] = gate.digest(
            self.manifest["binding"]
        )
        self.manifest["evidence"][0]["requirement"] = "E-RETENTION"
        self.assertEqual(self.cli()[0], 1)

    def test_partial_pr_cancelled_and_stale_ci(self):
        original = self.ci_snapshot()
        for key, value in (
            ("event", "pull_request"),
            ("event", "workflow_dispatch"),
            ("status", "in_progress"),
            ("conclusion", "cancelled"),
            ("jobs", original["jobs"][:-1]),
            ("inventories", original["inventories"][:-1]),
            ("head", "b" * 40),
        ):
            with self.subTest(key=key, value=value):
                snapshot = copy.deepcopy(original)
                snapshot[key] = value
                state, _ = gate.ci_assessment(snapshot, self.manifest["binding"])
                self.assertNotEqual(state, "PASS")
                self.assertNotEqual(state, "UNVERIFIED_CI_SNAPSHOT")

    def test_complete_ci_snapshot_is_not_authenticated(self):
        row = self.add_ci(self.ci_snapshot())
        self.assertEqual(row["status"], "INCONCLUSIVE")
        self.assertEqual(row["reason"], "UNVERIFIED_CI_SNAPSHOT")

    def test_masked_jobs_skips_and_inventory_mismatch(self):
        for field, value in (
            ("failed", 1),
            ("errors", 1),
            ("xfailed", 1),
            ("skipped", 1),
            ("executed_sha256", "c" * 64),
            ("passed", 0),
        ):
            snapshot = self.ci_snapshot()
            snapshot["inventories"][0][field] = value
            with self.subTest(field=field):
                self.assertNotEqual(
                    gate.ci_assessment(snapshot, self.manifest["binding"])[0], "PASS"
                )
        snapshot = self.ci_snapshot()
        snapshot["jobs"][0]["failures_masked"] = True
        self.assertEqual(
            gate.ci_assessment(snapshot, self.manifest["binding"])[0], "FAIL"
        )

    def test_duplicate_ci_jobs_rejected(self):
        snapshot = self.ci_snapshot()
        snapshot["jobs"][0] = snapshot["jobs"][1]
        with self.assertRaises(Rejected):
            self.add_ci(snapshot)

    def test_forged_approval_bytes_and_ids_never_authorize(self):
        self.add_evidence(
            "E-CUSTOMER", data=b'{"signed":true,"approved":true,"decision":"GO"}'
        )
        self.assertEqual(self.row("E-CUSTOMER")["status"], "INCONCLUSIVE")
        self.assert_no_admission(self.assess())

    def test_synthetic_pass_cannot_close_legal_content_restore(self):
        for key in ("E-POLICY", "D-EXPERT", "B-RESTORE", "D-FTS"):
            self.add_evidence(key, scope="LOCAL_SYNTHETIC")
            self.assertEqual(
                self.row(key)["reason"], "SYNTHETIC_CANNOT_CLOSE_REAL_GATE"
            )
            self.assertEqual(self.row(key)["status"], "INCONCLUSIVE")

    def test_missing_target_load_is_blocked_even_declared_pass(self):
        self.check("C-TARGET-LOAD")["status"] = "PASS"
        self.assertEqual(self.row("C-TARGET-LOAD")["status"], "BLOCKED")
        self.assertEqual(
            self.row("C-TARGET-LOAD")["reason"], "TARGET_MEASUREMENT_MISSING"
        )

    def test_existing_load_validator_and_target_scope_are_reused(self):
        self.bind_complete()
        self.add_evidence("C-TARGET-LOAD", "CI_SYNTHETIC", self.load_report(), "load")
        self.assertEqual(
            self.row("C-TARGET-LOAD")["reason"], "SYNTHETIC_CANNOT_CLOSE_TARGET"
        )
        artifact = self.manifest["artifacts"][0]["id"]
        report = gate.parse(self.records[artifact])
        report["operations"]["button"]["threshold_ms"] = 10000
        self.records[artifact] = gate.canonical(report)
        with self.assertRaises(ValueError):
            self.assess()

    def test_even_valid_target_report_cannot_authenticate_event(self):
        self.bind_complete()
        report = self.load_report(target=True)
        self.assertEqual(report["gates"]["TARGET_LOAD"], "PASS")
        self.add_evidence("C-TARGET-LOAD", "TARGET", report, "load")
        self.assertEqual(
            self.row("C-TARGET-LOAD")["reason"], "UNVERIFIED_TARGET_SNAPSHOT"
        )
        self.assert_no_admission(self.assess())

    def test_load_wrong_images_fail(self):
        self.bind_complete()
        report = self.load_report()
        report["identities"]["backend"] = "sha256:" + "c" * 64
        self.add_evidence("C-TARGET-LOAD", "TARGET", report, "load")
        self.assertEqual(self.row("C-TARGET-LOAD")["status"], "FAIL")

    def test_absent_offsite_restore_and_legal_policy_remain_blocked(self):
        for key in (
            "B-OFFSITE",
            "B-KEYS",
            "B-JOURNAL",
            "B-RESTORE",
            "B-RPO-RTO",
            "E-POLICY",
            "E-RETENTION",
            "E-ADR",
        ):
            self.assertEqual(self.row(key)["status"], "BLOCKED")

    def test_open_p0_p1_defects_fail_and_empty_triage_is_unverified(self):
        self.assertEqual(self.row("A-DEFECTS")["status"], "BLOCKED")
        for severity in ("P0", "P1"):
            self.manifest["defects"]["entries"] = [
                dict(id=opaque(1000), severity=severity, state="OPEN", feature="core")
            ]
            self.assertEqual(self.row("A-DEFECTS")["status"], "FAIL")

    def test_closed_defects_require_independent_triage(self):
        self.manifest["defects"]["entries"] = [
            dict(id=opaque(1000), severity="P0", state="CLOSED", feature="core")
        ]
        self.assertEqual(self.row("A-DEFECTS")["status"], "BLOCKED")

    def test_target_latency_failure_is_preserved(self):
        self.bind_complete()
        report = self.load_report(target=True)
        for field in ("all_attempt_ms", "success_ms"):
            report["operations"]["button"][field] = dict.fromkeys(
                ("p50", "p95", "p99", "maximum"), 301
            )
        report["operations"]["button"]["gate"] = "FAIL"
        report["gates"] = derive_gates(report)
        self.add_evidence("C-TARGET-LOAD", "TARGET", report, "load")
        self.assertEqual(self.row("C-TARGET-LOAD")["status"], "FAIL")
        self.assert_no_admission(self.assess())

    def test_missing_ci_or_load_report_cannot_mean_complete_evidence(self):
        for key, reason in (
            ("A-CI", "CI_SNAPSHOT_MISSING"),
            ("C-TARGET-LOAD", "TARGET_REPORT_MISSING"),
        ):
            self.add_evidence(key)
            self.assertEqual(self.row(key)["reason"], reason)
        self.assertEqual(self.assess()["packet_completeness"], "INCOMPLETE")

    def test_historical_ci_is_scoped_and_does_not_close_new_sha(self):
        result = self.assess()
        history = result["historical_baseline"]
        self.assertFalse(history["valid_for_current_candidate"])
        self.assertEqual(history["scope"], "HISTORICAL_CI_SYNTHETIC")
        self.assertEqual(history["button"]["status"], "FAIL")
        self.assertEqual(history["order"]["status"], "FAIL")
        self.assertEqual(self.row("A-CI")["status"], "NOT RUN")
        snapshot = self.ci_snapshot()
        snapshot["run_id"] = 42962729215
        gate.validate(snapshot, gate.CI_SCHEMA)

    def test_requirement_catalog_preserves_normative_dod_and_nine_jobs(self):
        plan = (gate.ROOT / "IMPLEMENTATION_PLAN.md").read_text()
        cards = re.findall(
            r"^#### (MVP-\d+) — [^\n]+\n(.*?)(?=^#### |^### |\Z)",
            plan,
            re.M | re.S,
        )
        actual = {}
        for key, body in cards:
            if re.search(r"\*\*Приоритет:\*\* P[01]", body) and key != "MVP-050":
                actual[key] = re.search(
                    r"\*\*DoD:\*\* (.*?)(?=\n- |\n\n|\Z)", body, re.S
                )[1].strip()
        self.assertEqual(
            actual,
            {k: v["dod"] for k, v in gate.CATALOG.items() if k.startswith("MVP-")},
        )
        workflow = (gate.ROOT / ".github/workflows/ci.yml").read_text()
        self.assertEqual(
            set(re.findall(r"^    name: (.+)$", workflow, re.M)), set(gate.JOBS)
        )

    def test_reduced_pilot_without_scope_decision_rejected(self):
        self.manifest["pilot"] = "REDUCED_PILOT"
        self.assertEqual(self.cli()[0], 1)

    def test_exclusions_require_endpoints_and_not_applicable_checks(self):
        self.reduced()
        self.manifest["scope"]["exclusions"][0]["disabled_endpoints"].pop()
        self.assertEqual(self.cli()[0], 1)

    def test_safety_privacy_restore_and_included_commerce_cannot_be_excluded(self):
        original = copy.deepcopy(self.manifest)
        for key in ("MVP-005", "MVP-010", "MVP-039", "E-POLICY", "B-RESTORE"):
            with self.subTest(key=key):
                self.manifest = copy.deepcopy(original)
                self.reduced()
                self.manifest["scope"]["exclusions"][0]["checks"].append(key)
                self.check(key)["status"] = "OUT_OF_SCOPE_APPROVED"
                self.assertEqual(self.cli()[0], 1)

    def test_unverified_scope_decision_is_not_approved(self):
        keys = self.reduced()
        for key in keys:
            self.assertEqual(self.row(key)["status"], "INCONCLUSIVE")
            self.assertEqual(self.row(key)["reason"], "SCOPE_DECISION_UNVERIFIED")
        self.assert_no_admission(self.assess())

    def test_excluded_function_is_never_pass(self):
        keys = self.reduced()
        self.check(keys[0])["status"] = "PASS"
        self.assertEqual(self.cli()[0], 1)

    def test_exclusion_cannot_hide_enabled_mode(self):
        self.reduced()
        self.manifest["binding"]["modes"]["chat"] = True
        self.manifest["evidence"][0]["binding_sha256"] = gate.digest(
            self.manifest["binding"]
        )
        self.assertEqual(self.cli()[0], 1)

    def test_full_and_reduced_modes_not_supported_by_current_controller(self):
        self.assertEqual(
            self.row("MODE-SUPPORTED")["reason"], "UNSUPPORTED_RELEASE_MODES"
        )
        self.reduced()
        self.assertEqual(self.row("MODE-SUPPORTED")["status"], "BLOCKED")
        self.manifest["binding"]["modes"] = copy.deepcopy(gate.MODES)
        # Even the exact existing profile only permits local synthetic data.
        self.manifest["evidence"][0]["binding_sha256"] = gate.digest(
            self.manifest["binding"]
        )
        self.assertEqual(
            self.row("MODE-SUPPORTED")["reason"], "UNSUPPORTED_RELEASE_MODES"
        )

    def test_no_automatic_go_for_self_authored_all_pass_file(self):
        self.bind_complete()
        for row in self.manifest["checks"]:
            self.add_evidence(row["id"])
        self.manifest["defects"]["review"] = self.check("A-DEFECTS")["evidence"]
        self.assert_no_admission(self.assess())
        self.assertEqual(self.cli()[0], 0)

    def test_private_bytes_never_appear_in_public_report(self):
        self.add_evidence("E-POLICY")
        code, result = self.cli()
        self.assertEqual(code, 0)
        raw = gate.canonical(result)
        self.assertNotIn(CANARY, raw)
        self.assertNotIn(opaque(2).encode(), raw)
        self.assertNotIn(b"artifact1.json", raw)

    def test_dirty_checkout_is_rejected(self):
        (self.repo / "untracked").write_text("SYNTHETIC ONLY")
        self.assertEqual(self.cli()[0], 1)

    def test_private_input_modes_links_traversal_and_changes(self):
        self.assertEqual(self.cli()[0], 0)
        path = self.root / "checklist.json"
        path.chmod(0o644)
        with self.assertRaises(Rejected):
            packet = gate.PrivateInput(self.root, self.repo)
            try:
                packet.read("checklist.json")
            finally:
                packet.close()
        path.chmod(0o600)
        packet = gate.PrivateInput(self.root, self.repo)
        try:
            with self.assertRaises(Rejected):
                packet.read("../checklist.json")
            packet.read("checklist.json")
            path.write_bytes(CANARY)
            with self.assertRaises(Rejected):
                packet.finish()
        finally:
            packet.close()
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                other = self.root / kind
                if kind == "symlink":
                    other.symlink_to(path)
                else:
                    os.link(path, other)
                packet = gate.PrivateInput(self.root, self.repo)
                try:
                    with self.assertRaises((OSError, Rejected)):
                        packet.read(kind)
                finally:
                    packet.close()
                other.unlink()

    def test_duplicate_json_keys_nan_unknown_approval_and_closed_parser(self):
        for raw in (b'{"format":1,"format":2}', b'{"x":NaN}', b'{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(Rejected):
                gate.parse(raw)
        self.manifest["checks"][0]["evidence"] = CANARY.decode()
        self.assertEqual(self.cli()[0], 1)

    def test_cli_does_not_invoke_mutations_network_or_db(self):
        # All subprocesses are the reused local, read-only Git metadata calls.
        original = subprocess.run
        calls = []

        def observed(args, **kwargs):
            calls.append(args)
            self.assertEqual(args[0], "git")
            self.assertIn("--no-optional-locks", args)
            self.assertTrue(
                any(a in args for a in ("status", "rev-parse", "merge-base"))
            )
            return original(args, **kwargs)

        with patch("subprocess.run", side_effect=observed):
            self.assertEqual(self.cli()[0], 0)
        self.assertTrue(calls)

    def test_committed_schema_and_fixture_match_runtime(self):
        self.assertEqual(
            json.loads((gate.DATA / "checklist.schema.json").read_bytes()),
            gate.CHECKLIST_SCHEMA,
        )
        self.assertEqual(
            json.loads((gate.DATA / "ci-snapshot.schema.json").read_bytes()),
            gate.CI_SCHEMA,
        )
        gate.validate(
            json.loads((gate.DATA / "synthetic.checklist.json").read_bytes()),
            gate.CHECKLIST_SCHEMA,
        )
        self.assertEqual(
            set(gate.CATALOG),
            {f"MVP-{i:03}" for i in (*range(1, 50), 54, 55, 56)}
            | {k for k in gate.CATALOG if not k.startswith("MVP-")},
        )
        for row in gate.REQUIREMENTS:
            self.assertTrue(set(row["depends_on"]) <= set(gate.CATALOG))


if __name__ == "__main__":
    unittest.main()
