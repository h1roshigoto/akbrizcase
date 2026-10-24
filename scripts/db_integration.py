"""Run a fresh PostgreSQL 16 stack with random secrets and guaranteed cleanup.

No URLs or credentials are accepted from the caller. Only a unique project with
our test label may be removed by the recovery command after an interrupted run.
"""

import json
import os
import secrets
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

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
            if not project.startswith("akbrizcase-dbtest-"):
                raise RuntimeError(f"Refusing to remove a non-test {resource}")
            projects.add(project)
    # Compose needs existing secret paths even for down; a fresh temp directory
    # supplies placeholders, whose values are never used to connect to a DB.
    for project in projects:
        run(project, cleanup_only=True)
    print("Removed interrupted DB test projects only")


def run(project: str, *, cleanup_only: bool = False) -> None:
    with tempfile.TemporaryDirectory(prefix="akbrizcase-dbtest-") as temporary:
        directory = Path(temporary)
        for name in ("postgres_password", "migrator_password", "runtime_password"):
            path = directory / name
            path.write_text(secrets.token_urlsafe(48) + "\n")
            # The parent is 0700 on the host; only selected files are mounted.
            path.chmod(0o444)
        environment = os.environ.copy()
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

        def compose(*args: str) -> None:
            subprocess.run([*prefix, *args], env=environment, check=True)

        try:
            if not cleanup_only:
                compose("build", "tests")
                compose("up", "-d", "--wait", "--wait-timeout", "120", "postgres-test")
                compose("run", "--rm", "tests")
        except subprocess.CalledProcessError:
            compose("logs", "--no-color", "--tail", "80", "postgres-test")
            raise
        finally:
            compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    if sys.argv[1:] == ["reset"]:
        reset()
    elif not sys.argv[1:]:
        run("akbrizcase-dbtest-" + uuid.uuid4().hex[:12])
    else:
        sys.exit("Usage: db_integration.py [reset]")
