"""Run a fresh PostgreSQL 16 stack with random secrets; preserve its volumes.

No URLs or credentials are accepted from the caller. Only a unique project with
our test label may be stopped by recovery. Neither normal nor reset deletes data.
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import uuid
from functools import partial
from pathlib import Path

from ci_inventory import record_verbose
from ci_suites import command, measure, parallel
from synthetic_stack import SyntheticStack, isolated_environment

LABEL = "com.akbrizcase.db-integration"
SUITES = {
    "clean-upgrade": ["tests/integration/test_kb_clean_upgrade.py"],
    "images": [
        "tests/integration/test_catalog_image_binding.py",
        "tests/integration/test_admin_images.py",
    ],
    "lifecycle": ["tests/integration/test_kb_lifecycle.py"],
    "applicability": ["tests/integration/test_catalog_applicability.py"],
    "trees": [
        "tests/integration/test_diagnostic_trees.py",
        "tests/integration/test_tree_lifecycle.py",
    ],
    "chat": [
        "tests/integration/test_chat_schema.py",
        "tests/integration/test_chat_sessions.py",
    ],
}
# Preserve the legacy selection, including its existing clean-upgrade skip.
# The same clean-upgrade test actually executes in its dedicated fresh DB.
SUITES["core"] = [
    "--ignore=" + path
    for name, paths in SUITES.items()
    if name != "clean-upgrade"
    for path in paths
]


def reset() -> None:
    projects = set()
    for resource, listing, inspection, labels_key in (
        ("container", ["docker", "ps", "-aq"], ["docker", "inspect"], "Config"),
        (
            "volume",
            ["docker", "volume", "ls", "-q"],
            ["docker", "volume", "inspect"],
            None,
        ),
        (
            "network",
            ["docker", "network", "ls", "-q"],
            ["docker", "network", "inspect"],
            None,
        ),
    ):
        result = subprocess.run(
            [*listing, "--filter", f"label={LABEL}=true"],
            check=True,
            text=True,
            capture_output=True,
        )
        for identifier in result.stdout.split():
            detail = subprocess.run(
                [*inspection, identifier], check=True, text=True, capture_output=True
            )
            item = json.loads(detail.stdout)[0]
            labels = item[labels_key]["Labels"] if labels_key else item["Labels"]
            project = labels["com.docker.compose.project"]
            if not re.fullmatch(r"akbrizcase-dbtest-[0-9a-f]{12}", project):
                raise RuntimeError(f"Refusing to stop a non-test {resource}")
            projects.add(project)
    # Compose needs existing secret paths even for down; a fresh temp directory
    # supplies placeholders, whose values are never used to connect to a DB.
    for project in projects:
        run(project, cleanup_only=True)
    print("Stopped labelled integration projects; all test volumes preserved")


def run(
    project: str,
    *,
    cleanup_only: bool = False,
    suite: str = "core",
    reuse_image: bool = False,
) -> None:
    with tempfile.TemporaryDirectory(prefix="akbrizcase-dbtest-") as temporary:
        directory = Path(temporary)
        for name in ("postgres_password", "migrator_password", "runtime_password"):
            path = directory / name
            path.write_text(secrets.token_urlsafe(48) + "\n")
            # The parent is 0700 on the host; only selected files are mounted.
            path.chmod(0o444)
        environment = isolated_environment(os.environ)
        environment["DB_TEST_SECRETS_DIR"] = temporary
        prefix = [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            "compose.integration.yml",
            "--project-name",
            project,
        ]
        # Optional local CA-only build override (no application credentials).
        if override := environment.get("DB_TEST_BUILD_OVERRIDE"):
            prefix += ["-f", override]

        def compose(*args: str) -> str:
            result = subprocess.run(
                [*prefix, *args],
                env=environment,
                check=True,
                text=True,
                capture_output=args[0] in ("config", "ps", "exec", "run"),
            )
            # Keep verbose node IDs intact when independent suites overlap.
            if args[0] == "run" and (
                not environment.get("CI_INVENTORY_DIR")
                or not record_verbose("db-" + suite, result.stdout)
            ):
                print(result.stdout, flush=True)
            return result.stdout or ""

        stack = SyntheticStack(project, compose, environment)
        prefix += ["-f", str(stack.write_override(directory))]
        try:
            if not cleanup_only:
                stack.claim()
                if not reuse_image:
                    compose("build", "tests")
                compose("up", "-d", "--wait", "--wait-timeout", "120", "postgres-test")
                # Materialize the isolated file volume before the ownership
                # guard verifies both persistent test volumes. No test runs yet.
                compose("create", "tests")
                stack.verify_database()
                if suite == "clean-upgrade":
                    compose(
                        "run",
                        "--rm",
                        "-e",
                        "DB_CLEAN_UPGRADE_ONLY=1",
                        "tests",
                        "python",
                        "-m",
                        "pytest",
                        "-m",
                        "integration",
                        "-p",
                        "no:cacheprovider",
                        "-v",
                        "tests/integration/test_kb_clean_upgrade.py",
                    )
                else:
                    compose(
                        "run",
                        "--rm",
                        "tests",
                        "python",
                        "-m",
                        "pytest",
                        "-m",
                        "integration",
                        "-p",
                        "no:cacheprovider",
                        "-v",
                        *SUITES[suite],
                    )
            else:
                stack.recover_for_stop()
        except subprocess.CalledProcessError as error:
            print(error.stdout or "", flush=True)
            print(error.stderr or "", file=sys.stderr, flush=True)
            compose("logs", "--no-color", "--tail", "80", "postgres-test")
            raise
        finally:
            stack.cleanup()


def exercise(suite: str, *, reuse_image: bool = False) -> None:
    run(
        "akbrizcase-dbtest-" + uuid.uuid4().hex[:12],
        suite=suite,
        reuse_image=reuse_image,
    )


def files_acceptance() -> None:
    # Build from this checkout with Docker's normal content-addressed cache.
    # No persistent cache or DB/files/session data is exported.
    with measure("files:runtime-build"):
        command("make", "build")
    command("make", "files-smoke")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=["reset"])
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--suite", choices=SUITES)
    selection.add_argument(
        "--images", dest="suite", action="store_const", const="images"
    )
    selection.add_argument("--trees", dest="suite", action="store_const", const="trees")
    parser.add_argument("--with-files", action="store_true")
    args = parser.parse_args()
    if (args.action and (args.suite or args.with_files)) or (
        args.suite and args.with_files
    ):
        parser.error("reset, a selected suite, and --with-files are separate modes")
    if args.action == "reset":
        reset()
    elif args.suite:
        with measure("db:" + args.suite):
            exercise(args.suite)
    else:
        # One build, then at most two guarded stacks including file acceptance.
        # Each suite still owns fresh PostgreSQL, secrets, network and volumes.
        with measure("db:build+clean-upgrade"):
            exercise("clean-upgrade")
        checks = [
            ("db:" + name, partial(exercise, name, reuse_image=True))
            for name in ("core", "images", "lifecycle")
        ]
        if args.with_files:
            checks.append(("files:clamav-sandbox", files_acceptance))
        checks.extend(
            ("db:" + name, partial(exercise, name, reuse_image=True))
            for name in ("applicability", "trees", "chat")
        )
        parallel(checks)


if __name__ == "__main__":
    main()
