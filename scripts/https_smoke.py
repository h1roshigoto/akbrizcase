"""Disposable TLS/browser acceptance. Requires Chromium, certutil and npm ci.

Temporarily trust the generated local CA in this operator's NSS database; remove
that one uniquely named entry afterward. Never disable TLS verification.
"""

import json
import os
import secrets
import socket
import ssl
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.request import HTTPSHandler, ProxyHandler, build_opener


def free_port() -> str:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return str(listener.getsockname()[1])


@contextmanager
def browser_trust():
    nickname = "akbrizcase-test-" + uuid.uuid4().hex
    # Chromium also supports the legacy location for backward compatibility.
    # Create it consistently so the pinned browser and newer system Chromium
    # use the same DB; preserve existing databases and entries.
    legacy = Path.home() / ".pki/nssdb"
    databases = [legacy]
    installed = []
    try:
        for database in databases:
            database.mkdir(parents=True, exist_ok=True)
            prefix = ["certutil", "-d", "sql:" + str(database)]
            if not (database / "cert9.db").exists():
                subprocess.run(
                    [*prefix, "-N", "--empty-password"], check=True, capture_output=True
                )
            subprocess.run(
                [*prefix, "-A", "-t", "C,,", "-n", nickname, "-i", ".certs/ca.crt"],
                check=True,
                capture_output=True,
            )
            installed.append(prefix)
        yield
    finally:
        for prefix in installed:
            subprocess.run(
                [*prefix, "-D", "-n", nickname], check=True, capture_output=True
            )


def main() -> None:
    if not Path(".certs/localhost.crt").is_file():
        raise SystemExit("Run make local-tls first")
    project = "akbrizcase-https-test-" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix=project) as temporary:
        environment = os.environ.copy()
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
        environment["HTTP_PORT"], environment["HTTPS_PORT"] = free_port(), free_port()
        origin = "https://localhost:" + environment["HTTPS_PORT"]
        environment["STAFF_TRUSTED_ORIGINS"] = origin
        prefix = [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            "docker-compose.yml",
            "-f",
            "compose.https.yml",
            "--project-name",
            project,
        ]

        def compose(*args: str, input_text: str | None = None) -> str:
            result = subprocess.run(
                [*prefix, *args],
                env=environment,
                input=input_text,
                text=True,
                capture_output=True,
                check=True,
                timeout=180,
            )
            return result.stdout

        passwords = {role: secrets.token_urlsafe(32) for role in ("admin", "manager")}
        try:
            compose("up", "--no-build", "--detach", "--wait", "--wait-timeout", "120")
            compose("run", "--rm", "migrate")
            # Explicit fixture creation in this disposable project only. The
            # application never auto-creates accounts or accepts piped CLI auth.
            setup = """
import json,sys
from app.core.db import Database,DatabaseSettings
from app.core.staff import bootstrap_admin,create_staff
p=json.load(sys.stdin)
db=Database(DatabaseSettings.from_environment())
with db.unit_of_work() as s:
 assert bootstrap_admin(s,login='browser-admin',password=p['admin']).ok
 assert create_staff(s,admin_login='browser-admin',admin_password=p['admin'],login='browser-manager',password=p['manager'],role='manager').ok
db.close()
"""
            compose(
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                setup,
                input_text=json.dumps(passwords),
            )
            tls = ssl.create_default_context(cafile=".certs/ca.crt")
            opener = build_opener(ProxyHandler({}), HTTPSHandler(context=tls))
            for path in ("/", "/healthz", "/readyz", "/admin/login"):
                with opener.open(origin + path, timeout=5) as response:
                    assert response.status == 200
            with browser_trust():
                browser_environment = environment.copy()
                browser_environment["NODE_EXTRA_CA_CERTS"] = str(
                    Path(".certs/ca.crt").resolve()
                )
                browser = subprocess.run(
                    ["node", "frontend/tests/https-auth.mjs"],
                    env=browser_environment,
                    input=json.dumps({"origin": origin, "passwords": passwords}),
                    text=True,
                    capture_output=True,
                    timeout=90,
                )
            if browser.returncode:
                # Assertion output contains no request bodies/cookies; redact
                # credentials defensively before surfacing tool diagnostics.
                diagnostic = browser.stderr
                for value in passwords.values():
                    diagnostic = diagnostic.replace(value, "[redacted]")
                raise RuntimeError("Browser acceptance failed: " + diagnostic)
            proof = json.loads(browser.stdout)
            logs = compose("logs", "--no-color", "api", "nginx", "postgres")
            audit = compose(
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "from app.core.db import Database,DatabaseSettings; from sqlalchemy import text; d=Database(DatabaseSettings.from_environment()); c=d.engine.connect(); print(c.execute(text('SELECT action,result,metadata FROM app.audit_events')).all()); c.close(); d.close()",
            )
            for secret in [*passwords.values(), *proof["secrets"]]:
                assert secret not in logs + audit, "Authentication material leaked"
            print(proof["result"])
            print(
                "PASS: certificate chain/hostname verification, HTTPS frontend/health/readiness, no secrets in application/proxy logs or audit"
            )
        finally:
            compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
