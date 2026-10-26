"""Run a fresh PostgreSQL 16 stack with random secrets; preserve its volumes.

No URLs or credentials are accepted from the caller. Only a unique project with
our test label may be stopped by recovery. Neither normal nor reset deletes data.
"""

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from synthetic_stack import SyntheticStack, isolated_environment

LABEL = "com.akbrizcase.db-integration"


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
    project: str, *, cleanup_only: bool = False, clean_upgrade_only: bool = False
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
                capture_output=args[0] in ("config", "ps", "exec"),
            )
            return result.stdout or ""

        stack = SyntheticStack(project, compose, environment)
        prefix += ["-f", str(stack.write_override(directory))]
        try:
            if not cleanup_only:
                stack.claim()
                compose("build", "tests")
                compose("up", "-d", "--wait", "--wait-timeout", "120", "postgres-test")
                # Materialize the isolated file volume before the ownership
                # guard verifies both persistent test volumes. No test runs yet.
                compose("create", "tests")
                stack.verify_database()
                if clean_upgrade_only:
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
                    compose("run", "--rm", "tests")
            else:
                stack.recover_for_stop()
        except subprocess.CalledProcessError:
            compose("logs", "--no-color", "--tail", "80", "postgres-test")
            raise
        finally:
            stack.cleanup()


if __name__ == "__main__":
    if sys.argv[1:] == ["reset"]:
        reset()
    elif not sys.argv[1:]:
        run("akbrizcase-dbtest-" + uuid.uuid4().hex[:12], clean_upgrade_only=True)
        run("akbrizcase-dbtest-" + uuid.uuid4().hex[:12])
    else:
        sys.exit("Usage: db_integration.py [reset]")
