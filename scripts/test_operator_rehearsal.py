"""Acceptance cannot pass on missing/skipped/failed child or restore evidence."""

import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import operator_rehearsal as rehearsal

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
                "DOCKER_HOST": "unix:///var/run/docker.sock",
            }
        )
        self.assertEqual(
            environment,
            {
                "HTTPS_PROXY": "platform-proxy",
                "DOCKER_CONFIG": "platform-config",
                "DB_TEST_BUILD_OVERRIDE": "trusted-ca-override",
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

    def execute(self, child, backup_output=None, dirty=False):
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
            result = rehearsal.main()
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
                result, output, runner = self.execute(lambda *a, **k: print(summary))
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
