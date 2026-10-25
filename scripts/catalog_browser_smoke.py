"""Real React/Nginx/API/PostgreSQL acceptance in a disposable seeded project.

Accept no database URL, project name or seed target from the caller. Temporary
credentials and unique volumes belong only to this run; volumes are preserved.
"""

import json
import os
import secrets
import subprocess
import tempfile
import uuid
from pathlib import Path

from https_smoke import free_port
from synthetic_stack import SyntheticStack, isolated_environment


def main() -> None:
    project = "akbrizcase-catalog-test-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix=project) as temporary:
        environment = isolated_environment(os.environ)
        for name, variable in (
            ("postgres_password", "POSTGRES_PASSWORD_FILE"),
            ("migrator_password", "MIGRATOR_PASSWORD_FILE"),
            ("runtime_password", "RUNTIME_PASSWORD_FILE"),
            ("staff_csrf_secret", "STAFF_CSRF_SECRET_FILE"),
            ("order_hmac_secret", "ORDER_HMAC_SECRET_FILE"),
        ):
            path = Path(temporary) / name
            path.write_text(secrets.token_urlsafe(48) + "\n")
            path.chmod(0o444)
            environment[variable] = str(path)
        environment["HTTP_PORT"] = free_port()
        prefix = [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            "docker-compose.yml",
            "--project-name",
            project,
        ]

        def compose(*args: str) -> str:
            result = subprocess.run(
                [*prefix, *args],
                env=environment,
                check=False,
                text=True,
                capture_output=True,
                timeout=180,
            )
            if result.returncode:
                raise RuntimeError(result.stderr)
            return result.stdout.strip()

        def sql(statement: str) -> str:
            return compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "akbrizcase_owner",
                "-d",
                "akbrizcase",
                "-v",
                "ON_ERROR_STOP=1",
                "-Atc",
                statement,
            )

        def browser(mode: str, selection: list) -> dict:
            result = subprocess.run(
                ["node", "frontend/tests/live-catalog.mjs"],
                env=environment,
                check=False,
                input=json.dumps(
                    {
                        "origin": "http://127.0.0.1:" + environment["HTTP_PORT"],
                        "mode": mode,
                        "selection": selection,
                    }
                ),
                text=True,
                capture_output=True,
                timeout=120,
            )
            if result.returncode:
                raise RuntimeError(
                    "Catalog browser acceptance failed: " + result.stderr
                )
            proof = json.loads(result.stdout)
            print(json.dumps(proof, ensure_ascii=False), flush=True)
            return proof

        stack = SyntheticStack(project, compose, environment)
        prefix += ["-f", str(stack.write_override(temporary))]
        try:
            stack.claim()
            compose("up", "--no-build", "--detach", "--wait", "--wait-timeout", "120")
            stack.verify_volumes()
            compose("run", "--rm", "migrate")
            assert sql("SELECT count(*) FROM app.catalog_items") == "0"
            # Only the owner of this newly created, uniquely named project marks
            # its database. The existing seed still enforces APP_ENV + DB marker.
            sql("COMMENT ON DATABASE akbrizcase IS 'akbrizcase:synthetic-seed:demo'")
            stack.verify_database()
            compose("run", "--rm", "-e", "APP_ENV=demo", "seed", "--mode", "demo")
            assert sql("SELECT count(*) FROM app.catalog_items") == "4"
            live = browser("live", [])
            selection = live["selection"]
            # Lifecycle changes stay inside the disposable database. No new
            # endpoints, fixtures in the bundle, or production writes.
            stack.verify_database()
            sql("""
                UPDATE app.catalog_items SET price=150.00, revision=revision+1,
                    description='<img src=x onerror="window.catalogInjected=true">'
                    WHERE sku='DEMO-P-001';
                UPDATE app.catalog_items SET status='archived'
                    WHERE sku='DEMO-S-001';
            """)
            browser("changed", selection)
            compose("stop", "postgres")
            browser("outage", selection)
            compose("up", "--no-build", "--detach", "--wait", "postgres")
            stack.verify_database()
            browser("recovered", selection)
            print("PASS: 4 Chromium catalog scenarios through Nginx/FastAPI/PostgreSQL")
        finally:
            stack.cleanup()


if __name__ == "__main__":
    main()
