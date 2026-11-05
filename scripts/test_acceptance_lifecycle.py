"""Acceptance entry points must preserve volumes on success and failure."""

import ast
import json
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import db_integration


class AcceptanceLifecycleTests(unittest.TestCase):
    def test_full_integration_schedules_every_suite_exactly_once(self):
        for with_files in (False, True):
            with (
                self.subTest(with_files=with_files),
                patch(
                    "sys.argv",
                    ["db_integration.py"] + (["--with-files"] if with_files else []),
                ),
                patch("db_integration.measure", lambda _: nullcontext()),
                patch("db_integration.exercise") as exercise,
                patch("db_integration.files_acceptance") as files,
                patch(
                    "db_integration.parallel",
                    lambda checks: [check() for _, check in checks],
                ),
            ):
                db_integration.main()
                self.assertCountEqual(
                    [call.args[0] for call in exercise.call_args_list],
                    db_integration.SUITES,
                )
                self.assertEqual(exercise.call_args_list[0].args, ("clean-upgrade",))
                for call in exercise.call_args_list[1:]:
                    self.assertEqual(call.kwargs, {"reuse_image": True})
                if with_files:
                    files.assert_called_once_with()
                else:
                    files.assert_not_called()

    def test_no_acceptance_entry_point_deletes_volumes(self):
        for name in (
            "synthetic_stack",
            "compose_smoke",
            "catalog_browser_smoke",
            "db_integration",
            "https_smoke",
            "request_browser_smoke",
            "catalog_admin_browser_smoke",
            "worker_smoke",
            "notification_retry_browser_smoke",
        ):
            tree = ast.parse(Path(__file__).with_name(name + ".py").read_text())
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call):
                    continue
                args = [a.value for a in call.args if isinstance(a, ast.Constant)]
                if args and args[0] in ("down", "rm"):
                    self.assertNotIn("--volumes", args, name)
                    self.assertNotIn("-v", args, name)
                self.assertNotEqual(args[:2], ["volume", "rm"], name)
                self.assertNotEqual(args[:2], ["volume", "prune"], name)

    def test_integration_reset_only_recovers_labelled_project(self):
        project = "akbrizcase-dbtest-0123456789ab"
        labels = {"com.docker.compose.project": project, db_integration.LABEL: "true"}
        with (
            patch("db_integration.subprocess.run") as command,
            patch("db_integration.run") as recover,
        ):
            command.side_effect = [
                SimpleNamespace(stdout="container"),
                SimpleNamespace(stdout=json.dumps([{"Config": {"Labels": labels}}])),
                SimpleNamespace(stdout=""),
                SimpleNamespace(stdout=""),
            ]
            db_integration.reset()
            recover.assert_called_once_with(project, cleanup_only=True)
            for call in command.call_args_list:
                self.assertNotIn("--volumes", call.args[0])
                self.assertNotIn("prune", call.args[0])

    def test_integration_reset_rejects_user_resource(self):
        labels = {
            "com.docker.compose.project": "user-project",
            db_integration.LABEL: "true",
        }
        with (
            patch("db_integration.subprocess.run") as command,
            patch("db_integration.run") as recover,
        ):
            command.side_effect = [
                SimpleNamespace(stdout="container"),
                SimpleNamespace(stdout=json.dumps([{"Config": {"Labels": labels}}])),
            ]
            with self.assertRaises(RuntimeError):
                db_integration.reset()
            recover.assert_not_called()
