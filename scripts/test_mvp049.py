"""Synthetic guards: hostile packets, offline snapshots, no false admission."""

import contextlib
import copy
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mvp049_content_gate as gate
import mvp049_rehearsal as rehearsal
from kb_acceptance_result import CORPUS_SHA256
from operator_rehearsal import STAGES

FIXTURE = gate.ROOT / "content/pilot/synthetic"
CANARY = "private-person@example.invalid /protected/secret token=not-a-real-token"


class ContentGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mvp049-test-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        for argv in (
            ["git", "init", "-q", str(self.repo)],
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "user.name=SYNTHETIC",
                "-c",
                "user.email=synthetic@example.invalid",
                "commit",
                "-qm",
                "SYNTHETIC ONLY",
                "--allow-empty",
            ],
        ):
            subprocess.run(argv, check=True, capture_output=True)
        self.head = gate.git(self.repo, "rev-parse", "HEAD")
        self.packet = root / "input"
        self.packet.mkdir(mode=0o700)
        self.output = root / "output"
        self.manifest = json.loads((FIXTURE / "manifest.template.json").read_bytes())
        self.manifest["git"].update(head=self.head, base=self.head)
        for entry in self.manifest["artifacts"]:
            path = self.packet / entry["filename"]
            path.write_bytes((FIXTURE / entry["filename"]).read_bytes())
            path.chmod(0o600)
        for tree in self.manifest["content"]["trees"]:
            tree["git_commit"] = self.head
        self.rebind()

    def store(self, name, value, kind):
        data = gate.canonical(value)
        filename = name + ".json"
        path = self.packet / filename
        path.write_bytes(data)
        path.chmod(0o600)
        entry = dict(id=name, filename=filename, sha256=gate.sha(data), kind=kind)
        self.manifest["artifacts"] = [
            a for a in self.manifest["artifacts"] if a["id"] != name
        ]
        self.manifest["artifacts"].append(entry)
        return name

    def rebind(self):
        # Test fixture maintenance only; deliberately never used for real inputs.
        content = self.manifest["content"]
        entities = [
            *content["catalog"]["models"],
            *content["catalog"]["items"],
            *content["images"],
            *content["documents"],
            *content["applicability"],
            *content["trees"],
            content["minimum"],
            content["search"],
        ]
        approvals = {a["id"]: a for a in self.manifest["approvals"]}
        for entity in entities:
            for key, value in entity.items():
                if key.endswith("_approval") and value is not None:
                    approvals[value]["subject_sha256"] = gate.subject(
                        entity, approvals[value]["scope"]
                    )

    def write_manifest(self):
        path = self.packet / "manifest.json"
        data = gate.canonical(self.manifest)
        path.write_bytes(data)
        path.chmod(0o600)
        return gate.sha(data)

    def run_cli(self, operation="check", checksum=None, extra=()):
        pin = self.write_manifest() if checksum is None else checksum
        args = [
            operation,
            "--repo",
            str(self.repo),
            "--input-root",
            str(self.packet),
            "--expected-sha256",
            pin,
            "--expected-head",
            self.head,
            "--expected-base",
            self.head,
            *extra,
        ]
        if operation == "report":
            args += ["--output-dir", str(self.output)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = gate.main(args)
        self.assertNotIn(CANARY, output.getvalue())
        self.assertNotIn(str(self.packet), output.getvalue())
        self.assertNotIn("subject_sha256", output.getvalue())
        result = json.loads(output.getvalue().split("MVP049_RESULT ")[-1])
        return code, result

    def full_receipt(self):
        return rehearsal.receipt(
            self.repo,
            self.head,
            {
                key: max(len(paths), 16)
                for key, paths in {**STAGES, **rehearsal.PILOT_STAGES}.items()
            },
            dict(
                corpus_sha256=CORPUS_SHA256,
                relevant=20,
                negative=20,
                hits=18,
                negative_safe=20,
                acl_incidents=0,
            ),
        )

    def add_measurement(self):
        corpus = json.loads((self.packet / "schema-corpus.json").read_bytes())
        rows = [
            dict(
                id=q["id"],
                returned_sections=q["expected_sections"],
                total=len(q["expected_sections"]),
                resolution="results" if q["group"] == "relevant" else "no_match",
                execution="PASS",
                acl_incidents=0,
            )
            for q in corpus["queries"]
        ]
        value = dict(
            format="MVP049_MEASUREMENT/v1",
            head=self.head,
            corpus_sha256=next(
                a["sha256"]
                for a in self.manifest["artifacts"]
                if a["id"] == "schema-corpus"
            ),
            rows=rows,
        )
        self.manifest["content"]["search"]["measurement_artifact"] = self.store(
            "measurement", value, "measurement"
        )
        self.rebind()
        return value

    def test_positive_validation_is_read_only_and_not_acceptance(self):
        self.write_manifest()
        before = {
            p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
            for p in self.packet.iterdir()
        }
        code, result = self.run_cli(
            checksum=gate.sha((self.packet / "manifest.json").read_bytes())
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["gates"]["MANIFEST_VALIDATION"], "PASS")
        self.assertEqual(result["gates"]["SYNTHETIC_CONTENT_REHEARSAL"], "NOT RUN")
        self.assertEqual(result["technical_checks"]["search"], "NOT RUN")
        self.assertEqual(result["technical_checks"]["state"], "NOT RUN")
        self.assertFalse(self.output.exists())
        self.assertEqual(
            before,
            {
                p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                for p in self.packet.iterdir()
            },
        )
        self.assertTrue(all(result["gates"][g] == "BLOCKED" for g in gate.GATES[2:]))

    def test_private_aggregate_is_exclusive_and_contains_no_content_or_approval(self):
        code, result = self.run_cli("report")
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        path = self.output / "result.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        before = path.read_bytes()
        self.assertEqual(json.loads(before), result)
        self.assertNotIn("SYNTHETIC-EXAMPLE", before.decode())
        self.assertEqual(self.run_cli("report")[0], 1)
        self.assertEqual(path.read_bytes(), before)

    def test_synthetic_pass_cannot_promote_any_real_gate(self):
        self.manifest["rehearsal_artifact"] = self.store(
            "workflow", self.full_receipt(), "rehearsal"
        )
        code, result = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(result["gates"]["SYNTHETIC_CONTENT_REHEARSAL"], "PASS")
        self.assertTrue(all(result["gates"][g] == "BLOCKED" for g in gate.GATES[2:]))
        self.assertEqual(result["MVP043_EXTERNAL"], "BLOCKED")
        self.assertEqual(result["MVP047_EXTERNAL"], "BLOCKED")
        self.manifest["classification"] = "REAL UNVERIFIED"
        self.assertEqual(self.run_cli()[0], 1)

    def test_missing_independent_rights_or_reviews_are_blocked(self):
        for entity, key, domain in (
            (
                self.manifest["content"]["catalog"]["items"][0],
                "business_approval",
                "catalog",
            ),
            (self.manifest["content"]["images"][0], "rights_approval", "images"),
            (self.manifest["content"]["documents"][0], "review_approval", "documents"),
            (
                self.manifest["content"]["documents"][0],
                "full_file_approval",
                "documents",
            ),
            (self.manifest["content"]["trees"][0], "review_approval", "trees"),
            (self.manifest["content"]["minimum"], "agreement_approval", "minimum"),
        ):
            with self.subTest(key=key):
                old = entity[key]
                entity[key] = None
                self.rebind()
                code, result = self.run_cli()
                self.assertEqual(code, 0)
                self.assertEqual(result["technical_checks"][domain], "BLOCKED")
                self.assertTrue(
                    all(result["gates"][g] == "BLOCKED" for g in gate.GATES[2:])
                )
                entity[key] = old
                self.rebind()

    def test_public_full_file_permission_does_not_follow_assistant_allowed(self):
        doc = self.manifest["content"]["documents"][0]
        doc["full_file_public_allowed"] = False
        doc["full_file_approval"] = None
        self.rebind()
        # The published dependent public tree also becomes unusable.
        self.assertEqual(self.run_cli()[0], 1)

    def test_minimum_must_be_agreed_and_fully_covered(self):
        self.manifest["content"]["minimum"]["scenarios"] = 3
        self.rebind()
        code, result = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(result["technical_checks"]["minimum"], "BLOCKED")

    def test_stale_revisions_bindings_and_approvals_fail(self):
        original = copy.deepcopy(self.manifest)
        mutations = [
            lambda c: c["catalog"]["items"][0].update(revision=4),
            lambda c: c["trees"][0].update(binding_sha256="f" * 64),
            lambda c: c["trees"][0].update(yaml_sha256="f" * 64),
            lambda c: c["documents"][0].update(sections_sha256="f" * 64),
            lambda c: c["documents"][0]["sections"][0].update(sha256="f" * 64),
            lambda c: c["applicability"][0].update(disclosure_allowed=False),
            lambda c: c["applicability"][0].update(review_approval=None),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.manifest = copy.deepcopy(original)
                mutate(self.manifest["content"])
                self.assertEqual(self.run_cli()[0], 1)

    def test_internal_or_revoked_tree_dependency_and_invalid_source_ref_fail(self):
        original = copy.deepcopy(self.manifest)
        for change in (
            dict(visibility="internal"),
            dict(status="retired"),
            dict(status="revoked"),
            dict(document_status="revoked"),
        ):
            self.manifest = copy.deepcopy(original)
            self.manifest["content"]["documents"][0].update(change)
            self.rebind()
            self.assertEqual(self.run_cli()[0], 1)
        self.manifest = copy.deepcopy(original)
        self.manifest["content"]["trees"][0]["nodes"][0]["source_refs"] = ["missing"]
        self.assertEqual(self.run_cli()[0], 1)

    def test_snapshot_must_match_exact_content_and_revision(self):
        state = dict(
            format="MVP049_STATE/v1",
            head=self.head,
            classification="SYNTHETIC ONLY",
            content=copy.deepcopy(self.manifest["content"]),
        )
        self.manifest["state_artifact"] = self.store("state", state, "state")
        code, result = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(result["technical_checks"]["state"], "PASS")
        self.assertEqual(result["gates"]["TARGET_CONTENT_VERIFICATION"], "BLOCKED")
        state["content"]["catalog"]["items"][0]["revision"] += 1
        self.store("state", state, "state")
        self.assertEqual(self.run_cli()[0], 1)

    def test_top5_recomputes_threshold_and_complete_denominator(self):
        measurement = self.add_measurement()
        for misses, expected in ((4, 0), (5, 1)):
            changed = copy.deepcopy(measurement)
            for row in changed["rows"][:misses]:
                row.update(returned_sections=[], total=0, resolution="no_match")
            self.store("measurement", changed, "measurement")
            self.assertEqual(self.run_cli()[0], expected)

    def test_partial_errored_spoofed_and_acl_measurement_fail(self):
        measurement = self.add_measurement()
        for mutation in (
            lambda m: m["rows"].pop(),
            lambda m: m["rows"].append(m["rows"][0]),
            lambda m: m.update(corpus_sha256="f" * 64),
            lambda m: m["rows"][0].update(execution="FAIL"),
            lambda m: m["rows"][0].update(acl_incidents=1),
            lambda m: m["rows"][0].update(returned_sections=["private-section"]),
            lambda m: m["rows"][-1].update(
                resolution="results",
                total=1,
                returned_sections=[
                    self.manifest["content"]["documents"][0]["sections"][0]["id"]
                ],
            ),
        ):
            changed = copy.deepcopy(measurement)
            mutation(changed)
            self.store("measurement", changed, "measurement")
            self.assertEqual(self.run_cli()[0], 1)

    def test_malformed_duplicate_key_oversize_depth_and_types_rejected(self):
        for payload in (
            b"{}",
            b'{"x":1,"x":2}',
            b'{"a":{"b":1,"b":2}}',
            b"NaN",
            b"1e9999",
            b"[" * 40 + b"0" + b"]" * 40,
            b" " * gate.MAX_JSON + b"{}",
            b'{"secret":"' + CANARY.encode() + b'"',
        ):
            with self.subTest(length=len(payload)):
                path = self.packet / "manifest.json"
                path.write_bytes(payload)
                path.chmod(0o600)
                self.assertEqual(self.run_cli(checksum=gate.sha(payload))[0], 1)
        for mutate in (
            lambda m: m.update(extra=CANARY),
            lambda m: m["content"]["catalog"]["items"][0].update(revision=True),
            lambda m: m["content"]["catalog"]["items"][0].update(price=1.0),
            lambda m: m["content"]["documents"][0].update(
                full_file_public_allowed="true"
            ),
            lambda m: m["artifacts"][0].update(filename="../private"),
        ):
            original = copy.deepcopy(self.manifest)
            mutate(self.manifest)
            self.assertEqual(self.run_cli()[0], 1)
            self.manifest = original

    def test_symlink_hardlink_fifo_and_unsafe_permissions_rejected(self):
        target = self.packet / self.manifest["artifacts"][0]["filename"]
        original = target.read_bytes()
        other = self.packet.parent / "protected-original"
        other.write_bytes(original)
        other.chmod(0o600)
        for kind in ("symlink", "hardlink", "fifo", "permissions"):
            target.unlink()
            if kind == "symlink":
                target.symlink_to(other)
            elif kind == "hardlink":
                os.link(other, target)
            elif kind == "fifo":
                os.mkfifo(target, 0o600)
            else:
                target.write_bytes(original)
                target.chmod(0o644)
            self.assertEqual(self.run_cli()[0], 1)
        target.unlink()
        target.write_bytes(original)
        target.chmod(0o600)
        self.packet.chmod(0o755)
        self.assertEqual(self.run_cli()[0], 1)

    def test_symlinked_parent_root_and_checkout_inputs_are_rejected(self):
        alias = self.packet.parent / "alias"
        alias.symlink_to(self.packet, target_is_directory=True)
        with self.assertRaises((OSError, gate.Rejected)):
            gate.PrivateInput(alias, self.repo)
        with self.assertRaises(gate.Rejected):
            gate.PrivateInput(self.repo, self.repo)

    def test_manifest_checksum_and_git_lineage_are_pinned_independently(self):
        self.assertEqual(self.run_cli(checksum="f" * 64)[0], 1)
        self.manifest["git"]["base"] = "f" * 40
        self.assertEqual(self.run_cli()[0], 1)
        self.manifest["git"]["base"] = self.head
        (self.repo / "unexpected").write_text("untracked")
        self.assertEqual(self.run_cli()[0], 1)

    def test_changed_inode_bytes_permissions_and_root_fail_finalizer(self):
        pin = self.write_manifest()
        path = self.packet / "manifest.json"
        for mutation in ("bytes", "inode", "permissions", "root"):
            with self.subTest(mutation=mutation):
                reader = gate.PrivateInput(self.packet, self.repo)
                try:
                    reader.read("manifest.json", pin)
                    if mutation == "bytes":
                        path.write_bytes(path.read_bytes() + b" ")
                    elif mutation == "inode":
                        data = path.read_bytes()
                        path.unlink()
                        path.write_bytes(data)
                        path.chmod(0o600)
                    elif mutation == "permissions":
                        path.chmod(0o644)
                    else:
                        self.packet.chmod(0o755)
                    with self.assertRaises(gate.Rejected):
                        reader.finish()
                finally:
                    reader.close()
                    self.packet.chmod(0o700)
                    pin = self.write_manifest()

    def test_reports_cannot_be_written_before_final_file_checks(self):
        with patch.object(
            gate.PrivateInput, "finish", side_effect=gate.Rejected(CANARY)
        ):
            self.assertEqual(self.run_cli("report")[0], 1)
        self.assertFalse(self.output.exists())

    def test_bad_rehearsal_receipt_cannot_assert_pass(self):
        value = self.full_receipt()
        for mutate in (
            lambda r: r.update(head="f" * 40),
            lambda r: r["stages"].pop("pilot-paths"),
            lambda r: r["stages"].update(knowledge=0),
            lambda r: r["fts"].update(negative_safe=19),
            lambda r: r["fts"].update(relevant=19),
            lambda r: r.update(PILOT_CONTENT_ADMISSION="PASS"),
            lambda r: r.update(approval=CANARY),
        ):
            changed = copy.deepcopy(value)
            mutate(changed)
            self.manifest["rehearsal_artifact"] = self.store(
                "workflow", changed, "rehearsal"
            )
            self.assertEqual(self.run_cli()[0], 1)
        for summary in (
            "",
            "=== 3 passed, 1 skipped in 1s ===",
            "=== 1 passed, 1 failed in 1s ===",
        ):
            with self.assertRaises(gate.Rejected):
                rehearsal.passed_count(summary)

    def test_all_paths_and_business_invariants_fail_after_rebinding(self):
        original = copy.deepcopy(self.manifest)
        for mutation in (
            lambda c: c["catalog"]["items"][0].update(price=None),
            lambda c: c["catalog"]["items"][1].update(price="1.00"),
            lambda c: c["catalog"]["items"][1].update(requires_shipping=True),
            lambda c: c["images"][0].update(output_sha256=None),
            lambda c: c["trees"][0]["nodes"][0].update(next=["observe", "finish"]),
            lambda c: c["trees"][0]["nodes"][1].update(source_refs=["missing"]),
            lambda c: c["trees"][0]["nodes"][1].update(
                recommendations=[{"item": "missing", "qty": 1}]
            ),
        ):
            self.manifest = copy.deepcopy(original)
            mutation(self.manifest["content"])
            tree = self.manifest["content"]["trees"][0]
            tree["inventory_sha256"] = gate.digest(
                {k: tree[k] for k in ("key", "version", "model", "start", "nodes")}
            )
            self.rebind()
            self.assertEqual(self.run_cli()[0], 1)
        self.manifest = copy.deepcopy(original)
        proof = json.loads((self.packet / "branching-paths.json").read_bytes())
        proof["paths"].pop()
        self.store("branching-paths", proof, "paths")
        self.assertEqual(self.run_cli()[0], 1)

    def test_internal_fact_is_separate_and_revoked_basis_becomes_unknown(self):
        content = self.manifest["content"]
        internal = copy.deepcopy(content["documents"][0])
        internal.update(
            code="SYNTHETIC-INTERNAL",
            visibility="internal",
            full_file_public_allowed=False,
            full_file_approval=None,
            review_approval=None,
        )
        internal["sections"][0]["id"] = "synthetic-internal-section"
        internal["section_inventory_sha256"] = gate.digest(internal["sections"])
        imported = json.loads((self.packet / "document-import.json").read_bytes())
        imported["code"] = internal["code"]
        imported["visibility"] = "internal"
        internal["import_artifact"] = self.store("internal-import", imported, "import")
        internal["import_sha256"] = gate.sha(gate.canonical(imported))
        content["documents"].append(internal)
        link = content["applicability"][0]
        link.update(section="synthetic-internal-section", public_sources=[])
        self.rebind()
        code, result = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(result["technical_checks"]["applicability"], "PASS")
        self.assertNotIn("synthetic-internal-section", gate.canonical(result).decode())
        internal["status"] = "revoked"
        link["status"] = "unknown"
        content["trees"] = []
        self.rebind()
        code, result = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(result["technical_checks"]["applicability"], "BLOCKED")
        self.assertEqual(result["technical_checks"]["minimum"], "BLOCKED")

    def test_rehearsal_materializer_checks_complete_private_packet(self):
        import shutil

        fixture = self.repo / "content/pilot/synthetic"
        shutil.copytree(FIXTURE, fixture)
        (fixture / "manifest.template.json").write_bytes(gate.canonical(self.manifest))
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "user.name=SYNTHETIC",
                "-c",
                "user.email=synthetic@example.invalid",
                "commit",
                "-qm",
                "SYNTHETIC schema",
            ],
            check=True,
            capture_output=True,
        )
        self.head = gate.git(self.repo, "rev-parse", "HEAD")
        directory = self.packet.parent / "rehearsal-packet"
        result = rehearsal.check_schema_fixture(
            self.repo, self.head, self.full_receipt(), directory
        )
        self.assertEqual(result["gates"]["MANIFEST_VALIDATION"], "PASS")
        self.assertEqual(result["gates"]["SYNTHETIC_CONTENT_REHEARSAL"], "PASS")
        self.assertIn("SCHEMA_FIXTURE_ONLY", result["codes"])
        self.assertTrue(all(result["gates"][g] == "BLOCKED" for g in gate.GATES[2:]))
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertTrue(
            all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in directory.iterdir())
        )

    def test_history_manifest_checksum_and_lineage_are_pinned(self):
        old = copy.deepcopy(self.manifest)
        self.manifest["git"]["previous_manifest_sha256"] = gate.sha(gate.canonical(old))
        self.store("previous-manifest", old, "previous")
        self.assertEqual(self.run_cli()[0], 0)
        entry = next(
            a for a in self.manifest["artifacts"] if a["id"] == "previous-manifest"
        )
        entry["sha256"] = "f" * 64
        self.assertEqual(self.run_cli()[0], 1)

    def test_yaml_parser_rejects_actual_bytes_or_reports_not_run(self):
        import base64
        import sys

        tree = self.manifest["content"]["trees"][0]
        items = {i["sku"]: i for i in self.manifest["content"]["catalog"]["items"]}
        self.assertIn(gate.verify_source(b"key: [", tree, items), {"FAIL", "NOT RUN"})
        request = {
            "yaml": base64.b64encode(
                (self.packet / "branching-yaml.bin").read_bytes()
            ).decode(),
            "items": {i["id"]: i["sku"] for i in items.values()},
            "expected": {
                k: tree[k] for k in ("key", "version", "model", "start", "nodes")
            },
        }
        # -S deliberately hides installed dependencies. No optimistic fallback.
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                str(gate.ROOT / "scripts/mvp049_source_check.py"),
            ],
            input=gate.canonical(request),
            capture_output=True,
            timeout=15,
        )
        self.assertEqual((result.returncode, result.stdout.strip()), (2, b"NOT RUN"))
        artifacts = {a["id"]: a for a in self.manifest["artifacts"]}
        records = {
            key: (self.packet / a["filename"]).read_bytes()
            for key, a in artifacts.items()
        }
        with patch.object(gate, "verify_source", return_value="NOT RUN"):
            checked = gate.check_content(self.manifest, artifacts, records)
        self.assertEqual(checked["trees"], "NOT RUN")

    def test_tree_source_git_commit_must_be_in_reviewed_lineage(self):
        self.manifest["content"]["trees"][0]["git_commit"] = "f" * 40
        self.rebind()
        self.assertEqual(self.run_cli()[0], 1)


if __name__ == "__main__":
    unittest.main()
