"""Exercise a disposable Compose project using synthetic data only.

The caller builds images first. Only this script's unique project/volumes are
removed; the regular demo project and its data are never touched.
"""

import json
import os
import re
import socket
import subprocess
import uuid
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener


def main() -> None:
    environment = os.environ.copy()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        environment["HTTP_PORT"] = str(listener.getsockname()[1])
    project = "akbrizcase-smoke-" + uuid.uuid4().hex[:10]
    prefix = ["docker", "compose", "--project-name", project]

    def compose(*args: str, input_text: str | None = None) -> str:
        result = subprocess.run(
            [*prefix, *args],
            input=input_text,
            text=True,
            capture_output=True,
            env=environment,
            check=True,
            timeout=180,
        )
        return result.stdout.strip()

    def inspect(service: str) -> dict:
        container = compose("ps", "--all", "--quiet", service)
        result = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        return json.loads(result.stdout)[0]

    def sql(query: str) -> str:
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
            query,
        )

    opener = build_opener(ProxyHandler({}))

    def http(path: str) -> tuple[int, str]:
        try:
            with opener.open(
                f"http://127.0.0.1:{environment['HTTP_PORT']}{path}",
                timeout=5,
            ) as response:
                return response.status, response.read().decode()
        except HTTPError as response:
            return response.code, response.read().decode()

    try:
        compose("config", "--quiet")
        compose("up", "--no-build", "--detach", "--wait", "--wait-timeout", "120")
        print("PASS: Compose started", flush=True)
        # Startup must never silently create or migrate the schema.
        assert http("/readyz") == (503, '{"status":"unavailable"}')
        compose("run", "--rm", "migrate")
        compose("run", "--rm", "migrate")
        assert http("/readyz") == (200, '{"status":"ok"}')
        assert sql("SELECT version_num FROM app.alembic_version") == "0002_mvp006_catalog"
        assert sql(
            "SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables "
            "WHERE schemaname = 'app'"
        ) == (
            "alembic_version,audit_events,catalog_items,equipment_models,"
            "staff_sessions,staff_users"
        )
        print(
            "PASS: explicit/repeat migrations and readiness through Nginx", flush=True
        )
        # The ordinary stack has no marker. Even an accidental APP_ENV=demo
        # cannot seed it. These invocations must fail without writing any rows.
        for arguments in (
            ("run", "--rm", "seed", "--mode", "demo"),
            ("run", "--rm", "-e", "APP_ENV=demo", "seed", "--mode", "demo"),
        ):
            try:
                compose(*arguments)
            except subprocess.CalledProcessError:
                pass
            else:
                raise AssertionError("Unmarked/production database accepted seed")
        assert sql("SELECT count(*) FROM app.catalog_items") == "0"
        # Only this unique, disposable smoke project is labelled by its owner.
        sql("COMMENT ON DATABASE akbrizcase IS 'akbrizcase:synthetic-seed:demo'")
        for _ in range(2):
            compose("run", "--rm", "-e", "APP_ENV=demo", "seed", "--mode", "demo")
        assert sql("SELECT count(*) FROM app.catalog_items") == "4"
        assert sql("SELECT count(*) FROM app.equipment_models") == "1"
        try:
            compose("run", "--rm", "-e", "APP_ENV=production", "seed", "--mode", "demo")
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("Production environment accepted seed")
        print("PASS: seed guards and idempotent synthetic demo catalog", flush=True)
        status, body = http("/healthz")
        assert status == 200 and json.loads(body) == {"status": "ok"}
        status, page = http("/")
        assert status == 200 and '<div id="root"></div>' in page
        assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page)
        assert assets
        bundles = [http(asset) for asset in assets]
        assert all(status == 200 for status, _ in bundles)
        assert any("B2B-портал" in body for _, body in bundles)
        status, body = http("/api/v1/not-implemented")
        assert status == 404 and json.loads(body) == {"detail": "Not Found"}
        print("PASS: Nginx health, UI/assets and API proxy", flush=True)

        api, worker, db, nginx = (
            inspect(s) for s in ("api", "worker", "postgres", "nginx")
        )
        assert api["Image"] == worker["Image"]
        for backend in (api, worker):
            mounted_secrets = {
                m["Destination"]
                for m in backend["Mounts"]
                if m["Destination"].startswith("/run/secrets/")
            }
            expected = {"/run/secrets/runtime_password"}
            if backend is api:
                expected.add("/run/secrets/staff_csrf_secret")
            assert mounted_secrets == expected
            assert all(
                "POSTGRES_PASSWORD" not in value for value in backend["Config"]["Env"]
            )
        print("PASS: runtime credentials only; CSRF key restricted to API", flush=True)
        for service in (api, worker, db):
            assert not service["HostConfig"]["PortBindings"]
        bindings = nginx["HostConfig"]["PortBindings"]["8080/tcp"]
        assert all(item["HostIp"] == "127.0.0.1" for item in bindings)
        assert not (
            nginx["NetworkSettings"]["Networks"].keys()
            & db["NetworkSettings"]["Networks"].keys()
        )
        for network in (project + "_web", project + "_data"):
            result = subprocess.run(
                ["docker", "network", "inspect", network],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            assert json.loads(result.stdout)[0]["Internal"] is True
        # Authenticate via TCP from the DB container, then check TCP reachability
        # from both backend services without adding a database driver to MVP-001.
        compose(
            "exec",
            "-T",
            "postgres",
            "sh",
            "-ec",
            'PGPASSWORD="$(cat /run/secrets/postgres_password)" '
            "psql -h postgres -U akbrizcase_owner -d akbrizcase "
            '-v ON_ERROR_STOP=1 -Atc "SELECT 1" >/dev/null',
        )
        try:
            compose(
                "exec",
                "-T",
                "postgres",
                "sh",
                "-ec",
                "PGPASSWORD=invalid-smoke-password psql -h postgres "
                "-U akbrizcase_owner -d akbrizcase -Atc 'SELECT 1'",
            )
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("PostgreSQL accepted an invalid TCP password")
        for service in ("api", "worker"):
            compose(
                "exec",
                "-T",
                service,
                "python",
                "-c",
                "import socket; socket.create_connection(('postgres', 5432), 3).close()",
            )
        assert sql("SHOW server_version").startswith("16.")
        print(
            "PASS: internal PostgreSQL 16/authentication; no DB/API/worker published ports",
            flush=True,
        )

        compose("stop", "postgres")
        assert http("/healthz")[0] == 200
        assert http("/readyz") == (503, '{"status":"unavailable"}')
        compose("up", "--no-build", "--detach", "--wait", "postgres")
        assert http("/readyz")[0] == 200
        print("PASS: DB outage preserves liveness and fails readiness", flush=True)

        marker = uuid.uuid4().hex
        compose(
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            "from pathlib import Path; p=Path('/var/lib/akbrizcase/files'); "
            "assert p.stat().st_mode & 0o777 == 0o700; "
            f"(p/'smoke-private.txt').write_text('{marker}')",
        )
        assert (
            compose(
                "exec",
                "-T",
                "worker",
                "python",
                "-c",
                "from pathlib import Path; print(Path('/var/lib/akbrizcase/files/smoke-private.txt').read_text())",
            )
            == marker
        )
        private_volume = next(
            m["Name"]
            for m in api["Mounts"]
            if m["Destination"] == "/var/lib/akbrizcase/files"
        )
        assert all(m.get("Name") != private_volume for m in nginx["Mounts"])
        status, body = http("/files/smoke-private.txt")
        assert status == 404 and marker not in body
        print(
            "PASS: private persistent file volume, shared by API/worker only",
            flush=True,
        )

        sql("CREATE TABLE mvp055_smoke (marker text NOT NULL)")
        sql(f"INSERT INTO mvp055_smoke VALUES ('{marker}')")
        compose("stop", "worker")
        assert inspect("worker")["State"]["ExitCode"] == 0
        assert "Worker stopped gracefully" in compose("logs", "--no-color", "worker")
        compose("stop")
        for service in ("api", "worker", "postgres", "nginx"):
            state = inspect(service)["State"]
            # Uvicorn re-raises SIGTERM after completing graceful ASGI shutdown.
            accepted_codes = (0, 143) if service == "api" else (0,)
            assert not state["Running"] and state["ExitCode"] in accepted_codes, (
                service,
                state,
            )
        assert "Application shutdown complete." in compose("logs", "--no-color", "api")
        compose("down")
        compose("up", "--no-build", "--detach", "--wait", "--wait-timeout", "120")
        assert sql("SELECT marker FROM mvp055_smoke") == marker
        assert (
            compose(
                "exec",
                "-T",
                "worker",
                "python",
                "-c",
                "from pathlib import Path; print(Path('/var/lib/akbrizcase/files/smoke-private.txt').read_text())",
            )
            == marker
        )
        assert http("/healthz")[0] == 200
        assert http("/readyz")[0] == 200
        print("PASS: graceful worker/stack stop; DB/files survive down/up", flush=True)
    finally:
        compose("down", "--volumes", "--remove-orphans")
        print("Removed disposable smoke project and synthetic volumes", flush=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.stderr.strip() or str(error)) from error
