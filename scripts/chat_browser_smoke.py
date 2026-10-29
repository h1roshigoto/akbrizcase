"""Real React -> Nginx -> FastAPI -> PostgreSQL, isolated synthetic content only."""

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from https_smoke import free_port
from synthetic_stack import SyntheticStack, isolated_environment, synthetic_secrets


def main():
    project = "akbrizcase-catalog-test-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix=project) as directory:
        environment = isolated_environment(os.environ)
        synthetic_secrets(directory, environment)
        environment["APP_ENV"] = "demo"
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

        def compose(*args, source=None):
            result = subprocess.run(
                [*prefix, *args],
                check=False,
                env=environment,
                text=True,
                capture_output=True,
                input=source,
                timeout=180,
            )
            if result.returncode:
                # Browser/seeding stdout never contains owner tokens or inputs.
                raise RuntimeError(result.stderr)
            return result.stdout.strip()

        stack = SyntheticStack(project, compose, environment)
        prefix += ["-f", str(stack.write_override(directory))]
        try:
            stack.claim()
            compose("up", "--no-build", "-d", "--wait", "--wait-timeout", "120")
            stack.verify_volumes()
            compose("run", "--rm", "migrate")
            stack.sql(
                "COMMENT ON DATABASE akbrizcase IS 'akbrizcase:synthetic-seed:demo'"
            )
            stack.verify_database()
            compose("run", "--rm", "seed", "--mode", "demo")
            # Script is stdin, not a mounted production module or an API bypass.
            config = json.loads(
                compose(
                    "exec",
                    "-T",
                    "api",
                    "python",
                    "-",
                    source=Path("scripts/chat_browser_fixture.py").read_text(),
                )
            )
            config["origin"] = "http://127.0.0.1:" + environment["HTTP_PORT"]
            stack.verify_database()
            browser = subprocess.run(
                ["node", "frontend/tests/live-chat.mjs"],
                check=False,
                env=environment,
                input=json.dumps(config),
                text=True,
                capture_output=True,
                timeout=150,
            )
            if browser.returncode:
                raise RuntimeError("Chat browser smoke failed: " + browser.stderr)
            print(browser.stdout, flush=True)
            assert stack.sql("SELECT count(*) FROM app.orders") == "0"
            print(
                "PASS: real chat knowledge/commerce/diagnose, reload, no automatic orders, no localStorage"
            )
        finally:
            stack.cleanup()


if __name__ == "__main__":
    main()
