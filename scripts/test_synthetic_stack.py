"""Negative acceptance-runner safeguards; no Docker or database writes."""

import copy
import json
import unittest
from unittest.mock import Mock

from synthetic_stack import LABEL, MARKER, SyntheticStack


class StackSafetyTests(unittest.TestCase):
    def setUp(self):
        self.project = "akbrizcase-https-test-0123456789"
        self.compose = Mock()
        self.stack = SyntheticStack(self.project, self.compose, {})
        self.config = {
            "services": {
                "postgres": {
                    "volumes": [
                        {
                            "target": "/var/lib/postgresql/data",
                            "type": "volume",
                            "source": "postgres_data",
                        }
                    ]
                }
            },
            "volumes": {
                name: {
                    "name": f"{self.project}_{name}",
                    "labels": {LABEL: self.project},
                }
                for name in ("app_files", "postgres_data")
            },
        }
        self.compose.return_value = json.dumps(self.config)
        self.stack.docker = Mock(return_value="")

    def claim(self):
        self.stack.claim()
        self.assertTrue(self.stack.claimed)

    def live(self):
        self.claim()
        self.compose.side_effect = lambda *args: (
            "container" if args[0] == "ps" else MARKER
        )

        def docker(*args):
            if args[:2] == ("volume", "ls"):
                return "\n".join(self.stack.names)
            if args[:2] == ("volume", "inspect"):
                return json.dumps(
                    [
                        {
                            "Labels": {
                                LABEL: self.project,
                                "com.docker.compose.project": self.project,
                            }
                        }
                    ]
                )
            return json.dumps(
                [
                    {
                        "Mounts": [
                            {
                                "Destination": "/var/lib/postgresql/data",
                                "Type": "volume",
                                "Name": f"{self.project}_postgres_data",
                            }
                        ]
                    }
                ]
            )

        self.stack.docker.side_effect = docker

    def test_reject_user_project(self):
        with self.assertRaises(RuntimeError):
            SyntheticStack("akbrizcase", self.compose, {})

    def test_reject_external_shared_and_unlabelled_volumes_before_start(self):
        for replacement in (
            {"external": True},
            {"name": "production_data"},
            {"labels": {}},
            {"labels": {LABEL: "other-run"}},
            {"driver_opts": {"type": "none", "o": "bind", "device": "/user/db"}},
            {"driver": "remote-driver"},
        ):
            with self.subTest(replacement=replacement):
                config = copy.deepcopy(self.config)
                config["volumes"]["postgres_data"].update(replacement)
                self.compose.return_value = json.dumps(config)
                with self.assertRaises(RuntimeError):
                    self.stack.claim()
                self.stack.cleanup()
                self.assertFalse(self.stack.claimed)
                self.assertFalse(
                    any(c.args[0] == "down" for c in self.compose.call_args_list)
                )

    def test_reject_existing_volume_without_cleanup(self):
        self.stack.docker.return_value = f"{self.project}_postgres_data"
        with self.assertRaises(RuntimeError):
            self.stack.claim()
        self.stack.cleanup()
        self.compose.assert_called_once_with("config", "--format", "json")

    def test_reject_user_mount_before_startup(self):
        self.config["services"]["postgres"]["volumes"][0].update(
            {"type": "bind", "source": "/user/postgres"}
        )
        self.compose.return_value = json.dumps(self.config)
        with self.assertRaises(RuntimeError):
            self.stack.claim()
        self.stack.cleanup()
        self.compose.assert_called_once_with("config", "--format", "json")

    def test_unclaimed_stack_cannot_access_volumes(self):
        with self.assertRaises(RuntimeError):
            self.stack.verify_volumes()
        self.stack.docker.assert_not_called()

    def test_docker_error_does_not_claim_or_cleanup(self):
        self.stack.docker.side_effect = RuntimeError("daemon unavailable")
        with self.assertRaises(RuntimeError):
            self.stack.claim()
        self.stack.cleanup()
        self.assertFalse(self.stack.claimed)

    def test_reject_existing_project_container(self):
        self.stack.docker.side_effect = ["", "container"]
        with self.assertRaises(RuntimeError):
            self.stack.claim()
        self.assertFalse(self.stack.claimed)

    def test_ignore_unrelated_user_volumes(self):
        self.stack.docker.side_effect = ["user_postgres_data\nuser_files", ""]
        self.claim()

    def test_marked_owned_database_is_allowed(self):
        self.live()
        self.stack.verify_database()

    def test_missing_marker_fails_closed(self):
        self.live()
        self.stack.sql = Mock(return_value="")
        with self.assertRaises(RuntimeError):
            self.stack.verify_database()

    def test_mounted_user_database_is_rejected(self):
        self.live()
        original = self.stack.docker.side_effect
        self.stack.docker.side_effect = lambda *args: (
            json.dumps(
                [
                    {
                        "Mounts": [
                            {
                                "Destination": "/var/lib/postgresql/data",
                                "Type": "volume",
                                "Name": "user_postgres_data",
                            }
                        ]
                    }
                ]
            )
            if args[0] == "inspect"
            else original(*args)
        )
        with self.assertRaises(RuntimeError):
            self.stack.verify_database()

    def test_changed_volume_label_prevents_cleanup(self):
        self.live()
        original = self.stack.docker.side_effect
        self.stack.docker.side_effect = lambda *args: (
            json.dumps([{"Labels": {LABEL: "other-run"}}])
            if args[:2] == ("volume", "inspect")
            else original(*args)
        )
        with self.assertRaises(RuntimeError):
            self.stack.cleanup()
        self.assertFalse(any(c.args[0] == "down" for c in self.compose.call_args_list))

    def test_cleanup_only_claimed_project(self):
        self.live()
        self.stack.cleanup()
        self.compose.assert_called_with("down")
        for call in self.compose.call_args_list:
            self.assertNotIn("--volumes", call.args)
            self.assertNotIn("-v", call.args)

    def test_shutdown_detects_disappearing_volumes(self):
        self.live()
        original = self.stack.docker.side_effect
        self.compose.side_effect = lambda *args: (
            setattr(self.stack.docker, "side_effect", lambda *args: "")
            if args == ("down",)
            else MARKER
        )
        with self.assertRaisesRegex(RuntimeError, "disappeared"):
            self.stack.cleanup()
        self.stack.docker.side_effect = original

    def test_other_acceptance_stacks_use_the_same_guard(self):
        for project, service, volumes in (
            ("akbrizcase-smoke-0123456789", "postgres", {"postgres_data", "app_files"}),
            (
                "akbrizcase-catalog-test-0123456789ab",
                "postgres",
                {"postgres_data", "app_files"},
            ),
            ("akbrizcase-dbtest-0123456789ab", "postgres-test", {"test_data"}),
        ):
            with self.subTest(project=project):
                stack = SyntheticStack(project, Mock(), {})
                self.assertEqual(stack.db_service, service)
                self.assertEqual(stack.volume_keys, volumes)

    def test_recovery_rejects_unowned_integration_container(self):
        stack = SyntheticStack("akbrizcase-dbtest-0123456789ab", self.compose, {})
        stack.docker = Mock(
            side_effect=[
                "container",
                json.dumps(
                    [
                        {
                            "Config": {
                                "Labels": {"com.docker.compose.project": stack.project},
                            }
                        }
                    ]
                ),
            ]
        )
        with self.assertRaises(RuntimeError):
            stack.recover_for_stop()
        self.assertFalse(stack.claimed)
        self.compose.assert_not_called()

    def test_recovery_preserves_integration_volumes(self):
        project = "akbrizcase-dbtest-0123456789ab"
        compose = Mock()
        stack = SyntheticStack(project, compose, {})
        labels = {
            LABEL: project,
            "com.docker.compose.project": project,
            "com.akbrizcase.db-integration": "true",
        }

        def docker(*args):
            if args[0] == "ps":
                return "container"
            if args[0] == "inspect":
                return json.dumps([{"Config": {"Labels": labels}}])
            if args[:2] == ("volume", "ls"):
                return project + "_test_data"
            return json.dumps([{"Labels": labels}])

        stack.docker = Mock(side_effect=docker)
        stack.recover_for_stop()
        stack.cleanup()
        compose.assert_called_once_with("down")

    def test_recovery_rejects_mismatched_volume_label(self):
        project = "akbrizcase-dbtest-0123456789ab"
        compose = Mock()
        stack = SyntheticStack(project, compose, {})
        stack.docker = Mock(
            side_effect=[
                "",
                project + "_test_data",
                project + "_test_data",
                json.dumps([{"Labels": {LABEL: "other"}}]),
            ]
        )
        with self.assertRaises(RuntimeError):
            stack.recover_for_stop()
        stack.cleanup()
        compose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
