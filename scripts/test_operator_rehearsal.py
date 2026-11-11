"""Acceptance cannot pass on missing/skipped/failed child or restore evidence."""

import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import mvp049_content_gate as content_gate
import mvp049_rehearsal as content_rehearsal
import operator_rehearsal as rehearsal
import test_kb_acceptance_result as corpus_cases

HEAD = "a" * 40
CANARY = "private-synthetic-secret@example.invalid"


def backup(**changes):
    return "BACKUP_EVIDENCE " + json.dumps(
        {
            "head": HEAD,
            "offsite": "BLOCKED",
            "target_rpo_rto": "BLOCKED",
            "new_order_admission": False,
            "smtp_disabled": True,
            "post_snapshot_number_protected": True,
            "incomplete_restore_blocked": True,
            "sequence_negative_cases": 15,
            **changes,
        }
    )


class OperatorRehearsalTests(unittest.TestCase):
    def test_environment_cannot_redirect_compose_database_smtp_or_daemon(self):
        environment = rehearsal.local_environment(
            {
                "COMPOSE_FILE": CANARY,
                "DB_HOST": CANARY,
                "SMTP_HOST": CANARY,
                "MIGRATION_DB_HOST": CANARY,
                "DOCKER_HOST": CANARY,
                "DOCKER_CONTEXT": CANARY,
                "DOCKER_TLS_VERIFY": CANARY,
                "HTTPS_PROXY": "platform-proxy",
                "DOCKER_CONFIG": "platform-config",
                "DB_TEST_BUILD_OVERRIDE": "trusted-ca-override",
            }
        )
        self.assertEqual(
            environment,
            {
                "HTTPS_PROXY": "platform-proxy",
                "DOCKER_CONFIG": "platform-config",
                "DB_TEST_BUILD_OVERRIDE": "trusted-ca-override",
                "DOCKER_HOST": "unix:///var/run/docker.sock",
            },
        )

    def test_restore_requires_exact_head_closed_admission_and_external_blockers(self):
        rehearsal.checked_backup(backup(), HEAD)
        for change in (
            {"head": "b" * 40},
            {"offsite": "PASS"},
            {"target_rpo_rto": "PASS"},
            {"new_order_admission": True},
            {"smtp_disabled": False},
            {"incomplete_restore_blocked": False},
            {"post_snapshot_number_protected": False},
            {"sequence_negative_cases": 0},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                rehearsal.checked_backup(backup(**change), HEAD)
        for invalid in ("", "{}", backup() + "\n" + backup()):
            with self.assertRaises(ValueError):
                rehearsal.checked_backup(invalid, HEAD)

    def execute(self, child, backup_output=None, dirty=False, pilot_content=False):
        def process(argv, **kwargs):
            output = ""
            if argv[:3] == ["git", "status", "--porcelain"]:
                output = " M unsafe" if dirty else ""
            elif argv[:3] == ["git", "rev-parse", "HEAD"]:
                output = HEAD
            else:
                output = backup() if backup_output is None else backup_output
            return subprocess.CompletedProcess(argv, 0, stdout=output)

        output = io.StringIO()
        with (
            patch.object(rehearsal.subprocess, "run", side_effect=process),
            patch.object(rehearsal, "run", side_effect=child) as runner,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            result = rehearsal.main(pilot_content=pilot_content)
        self.assertNotIn(CANARY, output.getvalue())
        return result, output.getvalue(), runner

    def test_all_stages_need_real_pass_and_guarded_fresh_projects(self):
        def child(*args, **kwargs):
            print("================ 3 passed in 1.00s ================")

        result, output, runner = self.execute(child)
        self.assertEqual(result, 0)
        self.assertEqual(runner.call_count, len(rehearsal.STAGES))
        projects = [call.args[0] for call in runner.call_args_list]
        self.assertEqual(len(set(projects)), len(projects))
        for call in runner.call_args_list:
            self.assertRegex(call.args[0], r"^akbrizcase-dbtest-[0-9a-f]{12}$")
            self.assertTrue(call.kwargs["capture_all"])
            self.assertEqual(call.kwargs["command_timeout"], 600)
        record = json.loads(output.split("OPERATOR_EVIDENCE ")[1])
        self.assertEqual(record["production"], "BLOCKED")
        self.assertEqual(record["human_handover"], "BLOCKED")

    def test_no_tests_skips_and_negative_failure_never_make_pass(self):
        for summary in (
            "",
            "=== 1 passed, 1 skipped in 1s ===",
            "=== 1 passed, 1 failed in 1s ===",
            "=== 1 passed, 1 xfailed in 1s ===",
        ):
            with self.subTest(summary=summary):
                result, output, runner = self.execute(
                    lambda *a, summary=summary, **k: print(summary)
                )
                self.assertEqual(result, 1)
                self.assertEqual(runner.call_count, 1)
                self.assertNotIn("OPERATOR_EVIDENCE", output)

        def failure(*args, **kwargs):
            raise subprocess.CalledProcessError(
                1, [CANARY], output=CANARY, stderr=CANARY
            )

        result, output, _ = self.execute(failure)
        self.assertEqual(result, 1)
        self.assertIn("OPERATOR_STAGE_FAIL staff", output)

    def test_content_failure_emits_closed_failed_result_without_receipt(self):
        result, output, _ = self.execute(
            lambda *a, **k: print("=== 1 failed ==="), pilot_content=True
        )
        self.assertEqual(result, 1)
        self.assertNotIn("MVP049_REHEARSAL", output)
        self.assertNotIn("OPERATOR_EVIDENCE", output)
        record = json.loads(output.split("MVP049_RESULT ")[1])
        self.assertEqual(record["gates"]["SYNTHETIC_CONTENT_REHEARSAL"], "FAIL")
        self.assertEqual(record["gates"]["PILOT_CONTENT_ADMISSION"], "BLOCKED")
        self.assertEqual(record["gates"]["PRODUCTION_ADMISSION"], "BLOCKED")

    def test_live_content_pass_requires_all_stages_and_checked_restore(self):
        # Controller-only unit test: no mocked DB acceptance is claimed here.
        for failure in (None, "stage", "skip", "xfail", "fts_head", "restore"):
            with self.subTest(failure=failure):
                self.check_content_case(failure)

    def check_content_case(self, failure):
        stage_names = {**rehearsal.STAGES, **content_rehearsal.PILOT_STAGES}
        valid_fts = corpus_cases.CorpusEvidenceTests().record(hits=18)
        observed = []
        original_check = rehearsal.checked_backup

        def checked_restore(output, head):
            value = original_check(output, head)
            observed.append("restore-validated")
            return value

        def child(*args, **kwargs):
            observed.append(kwargs["suite"])
            if kwargs["suite"] == "operator-pilot-paths" and failure in {
                "stage",
                "skip",
                "xfail",
            }:
                outcome = {
                    "stage": "failed",
                    "skip": "skipped",
                    "xfail": "xfailed",
                }[failure]
                print(f"=== 100 passed, 1 {outcome} in 1s ===")
                return
            if kwargs["suite"] == "kb-acceptance":
                measured = (
                    {**valid_fts, "head_sha": "b" * 40}
                    if failure == "fts_head"
                    else valid_fts
                )
                print("KB_ACCEPTANCE_RESULT " + json.dumps(measured))
            print("=== 100 passed in 1s ===")

        def fixture(*args):
            self.assertEqual(len(observed), len(stage_names) + 1)
            self.assertEqual(observed[-1], "restore-validated")
            result = content_gate.empty_result()
            result["gates"]["MANIFEST_VALIDATION"] = "PASS"
            result["gates"]["SYNTHETIC_CONTENT_REHEARSAL"] = "INCONCLUSIVE"
            result["codes"] = ["UNVERIFIED_REHEARSAL_RECEIPT"]
            return result

        with (
            patch.object(content_rehearsal, "git", return_value="b" * 40),
            patch.object(
                content_rehearsal, "check_schema_fixture", side_effect=fixture
            ) as schema,
            patch.object(rehearsal, "checked_backup", side_effect=checked_restore),
        ):
            code, output, runner = self.execute(
                child,
                pilot_content=True,
                backup_output=backup(incomplete_restore_blocked=False)
                if failure == "restore"
                else None,
            )
        result = json.loads(output.split("MVP049_RESULT ")[1])
        if failure is None:
            self.assertEqual(code, 0)
            self.assertEqual(runner.call_count, len(stage_names))
            self.assertEqual(result["gates"]["SYNTHETIC_CONTENT_REHEARSAL"], "PASS")
            self.assertIn("CURRENT_PROCESS_REHEARSAL", result["codes"])
            self.assertNotIn("UNVERIFIED_REHEARSAL_RECEIPT", result["codes"])
            schema.assert_called_once()
        else:
            self.assertEqual(code, 1)
            self.assertEqual(result["gates"]["SYNTHETIC_CONTENT_REHEARSAL"], "FAIL")
            self.assertNotIn("MVP049_REHEARSAL", output)
            schema.assert_not_called()
        self.assertTrue(
            all(result["gates"][g] == "BLOCKED" for g in content_gate.GATES[2:])
        )

    def test_dirty_checkout_and_false_backup_success_are_rejected(self):
        result, output, runner = self.execute(lambda *a, **k: None, dirty=True)
        self.assertEqual(result, 1)
        self.assertFalse(runner.called)
        self.assertNotIn("OPERATOR_EVIDENCE", output)
        result, output, _ = self.execute(
            lambda *a, **k: print("=== 1 passed in 1s ==="),
            backup_output=backup(new_order_admission=True),
        )
        self.assertEqual(result, 1)
        self.assertIn("OPERATOR_STAGE_FAIL backup", output)
        self.assertNotIn("OPERATOR_EVIDENCE", output)


if __name__ == "__main__":
    unittest.main()
