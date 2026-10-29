"""Real file acceptance in its own guarded Compose project; preserve all volumes."""

import json
import os
import socket
import subprocess
import tempfile
import uuid
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from ci_inventory import record_collection
from synthetic_stack import SyntheticStack, isolated_environment, synthetic_secrets


def main():
    with tempfile.TemporaryDirectory(prefix="akbrizcase-files-") as temporary:
        environment = isolated_environment(os.environ)
        synthetic_secrets(temporary, environment)
        environment["APP_ENV"] = "demo"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            environment["HTTP_PORT"] = str(listener.getsockname()[1])
        project = "akbrizcase-smoke-" + uuid.uuid4().hex[:10]
        prefix = [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            "docker-compose.yml",
            "-f",
            "compose.files-test.yml",
            "--project-name",
            project,
        ]
        if override := environment.get("FILES_TEST_BUILD_OVERRIDE"):
            prefix += ["-f", override]

        def compose(*args):
            print("FILES_STAGE: " + args[0], flush=True)
            return subprocess.run(
                [*prefix, *args],
                env=environment,
                check=True,
                text=True,
                capture_output=True,
                timeout=240,
            ).stdout.strip()

        def detail(service):
            return json.loads(stack.docker("inspect", compose("ps", "-aq", service)))[0]

        stack = SyntheticStack(project, compose, environment)
        prefix += ["-f", str(stack.write_override(temporary))]
        try:
            stack.claim()
            compose("build", "files-tests")
            compose("up", "--no-build", "-d", "--wait", "--wait-timeout", "120")
            stack.verify_volumes()
            compose("run", "--rm", "migrate")
            compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "akbrizcase_owner",
                "-d",
                "akbrizcase",
                "-c",
                "COMMENT ON DATABASE akbrizcase IS 'akbrizcase:synthetic-seed:demo'",
            )
            stack.verify_database()
            compose("run", "--rm", "seed", "--mode", "demo")
            snapshot_query = (
                "SELECT md5(string_agg(row_to_json(c)::text, ',' ORDER BY id)) "
                "FROM app.catalog_items c"
            )
            schema_query = (
                "SELECT md5(string_agg(table_name || column_name || data_type, "
                "',' ORDER BY table_name, ordinal_position)) "
                "FROM information_schema.columns WHERE table_schema='app'"
            )
            before = stack.sql(snapshot_query), stack.sql(schema_query)
            name = project + "-file-checks"
            print("CI_FILE_TEST_RESULTS_BEGIN", flush=True)
            execution_output = compose("run", "--name", name, "files-tests")
            print(execution_output, flush=True)
            print("CI_FILE_TEST_RESULTS_END", flush=True)
            # Keep execution unchanged; collect IDs only after every test passes.
            if environment.get("CI_INVENTORY_DIR"):
                collected_output = compose(
                    "run",
                    "--rm",
                    "files-tests",
                    "python",
                    "-m",
                    "pytest",
                    "-m",
                    "not integration",
                    "-p",
                    "no:cacheprovider",
                    "--collect-only",
                    "-q",
                    "tests/test_files.py",
                    "tests/test_files_acceptance.py",
                    "tests/test_catalog_images.py",
                )
                record_collection("files", collected_output, execution_output)
            test = json.loads(stack.docker("inspect", name))[0]
            host = test["HostConfig"]
            assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"]
            assert (
                host["CapDrop"] == ["ALL"]
                and "no-new-privileges:true" in host["SecurityOpt"]
            )
            assert test["Config"]["User"] == "10001:10001"
            assert not any(
                m["Destination"].startswith("/run/secrets") for m in test["Mounts"]
            )
            assert not any(
                "PASSWORD" in value or "TOKEN" in value
                for value in test["Config"]["Env"]
            )
            assert before == (stack.sql(snapshot_query), stack.sql(schema_query))
            assert (
                stack.sql("SELECT version_num FROM app.alembic_version")
                == "0008_mvp037_privacy"
            )
            assert stack.sql("SELECT count(*) FROM app.document_versions") == "0"
            api, nginx = detail("api"), detail("nginx")
            volume = next(
                m["Name"]
                for m in api["Mounts"]
                if m["Destination"] == "/var/lib/akbrizcase/files"
            )
            assert all(m.get("Name") != volume for m in nginx["Mounts"])
            key = compose(
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "from pathlib import Path; "
                "print(next(Path('/var/lib/akbrizcase/files/documents/verified')"
                ".iterdir()).name)",
            )
            opener = build_opener(ProxyHandler({}))
            for path in (
                "/files/" + key,
                "/api/v1/documents/" + key + "/file",
                "/documents",
                "/documents/quarantine/" + key,
                "/documents/verified/" + key,
                "/documents/receipts/" + key,
                "/images",
                "/images/quarantine/" + key + ".png",
                "/images/verified/" + key + ".png",
                "/images/receipts/" + key + ".png",
            ):
                try:
                    opener.open(
                        "http://127.0.0.1:" + environment["HTTP_PORT"] + path, timeout=5
                    )
                except HTTPError as error:
                    assert error.code == 404
                else:
                    raise AssertionError("Private file became accessible")
            print(
                "PASS: real ClamAV/sandbox, private volume/HTTP isolation, "
                "unchanged catalog/schema, no publication",
                flush=True,
            )
        finally:
            stack.cleanup()


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        # Tests contain synthetic data only. Never print compose config or secrets.
        print((error.stdout or "")[-12000:])
        raise SystemExit("FILES_ACCEPTANCE_FAILED") from None
