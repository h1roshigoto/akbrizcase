"""One bounded synthetic backup/restore job; retain volumes, stop every service.

Only local Docker, random fresh namespace, fresh credentials and internal network.
No caller-supplied database/volume/daemon selectors or production credentials.
"""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import uuid

REPO = Path(__file__).resolve().parents[1]
VOLUMES = {"source_data", "restore_data", "source_files", "restore_files", "synthetic_store", "external_journal"}


def run(argv, environment, *, timeout=180):
    return subprocess.run(argv, cwd=REPO, env=environment, capture_output=True,
                          text=True, check=True, timeout=timeout).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--reuse-image", help="Local immutable sha256 image ID for development")
    args = parser.parse_args()
    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith(("COMPOSE_", "DB_", "MIGRATION_DB_", "SMTP_", "BACKUP_TEST_")) or key in {
            "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}:
            environment.pop(key)
    docker = ["docker", "--host=unix:///var/run/docker.sock"]
    project = "akbriz-backup-" + uuid.uuid4().hex[:12]
    claimed = False
    with tempfile.TemporaryDirectory(prefix="akbriz-backup-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        prefix = [*docker, "compose", "--env-file", "/dev/null", "--project-name", project,
                  "-f", "compose.backup.yml"]

        def compose(*argv, timeout=180):
            return run([*prefix, *argv], environment, timeout=timeout)

        try:
            head = run(["git", "rev-parse", "HEAD"], environment)
            environment["BACKUP_TEST_HEAD"] = head
            environment["BACKUP_TEST_SECRETS"] = str(root)
            for name in ("postgres_password", "migrator_password", "runtime_password",
                         "restore_postgres_password", "restore_migrator_password", "restore_runtime_password"):
                path = root / name
                path.write_text(secrets.token_urlsafe(48))
                path.chmod(0o444)  # Selected bind files only; private 0700 parent.
            print("BACKUP_REHEARSAL_BUILD", flush=True)
            if args.reuse_image:
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.reuse_image):
                    raise ValueError
                image = args.reuse_image
            else:
                build = [*docker, "build", "--target", "backup", "--label",
                         "org.opencontainers.image.revision="+head, "--iidfile", str(root / "image.id")]
                if environment.get("CODEX_PROXY_CERT"):
                    build += ["--secret", "id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt"]
                run([*build, "backend"], environment, timeout=900)
                image = (root / "image.id").read_text().strip()
            environment["BACKUP_TEST_IMAGE"] = image
            config = json.loads(compose("config", "--format", "json"))
            if set(config["volumes"]) != VOLUMES or set(config["services"]) != {"postgres-test", "restore-postgres", "drill"}:
                raise ValueError
            for key, volume in config["volumes"].items():
                if (volume.get("external") or volume.get("driver_opts")
                        or volume.get("name") != project + "_" + key):
                    raise ValueError
            if run([*docker, "ps", "-aq", "--filter", "label=com.docker.compose.project="+project], environment):
                raise ValueError
            existing = set(run([*docker, "volume", "ls", "-q"], environment).splitlines())
            if any(project + "_" + key in existing for key in VOLUMES):
                raise ValueError
            for service in config["services"].values():
                if service.get("ports") or service.get("network_mode") or service.get("privileged"):
                    raise ValueError
            if not config["networks"]["closed"].get("internal"):
                raise ValueError
            claimed = True
            print("BACKUP_REHEARSAL_PG16", flush=True)
            compose("up", "-d", "--wait", "--wait-timeout", "120", "postgres-test", "restore-postgres")
            # Inspect the actual services before allowing any fixture/mutation.
            for service, expected in (("postgres-test", "source_data"), ("restore-postgres", "restore_data")):
                identifier = compose("ps", "--quiet", service)
                detail = json.loads(run([*docker, "inspect", identifier], environment))[0]
                mounts = [m for m in detail["Mounts"] if m["Destination"] == "/var/lib/postgresql/data"]
                if len(mounts) != 1 or mounts[0].get("Name") != project + "_" + expected:
                    raise ValueError
            print("BACKUP_REHEARSAL_ENCRYPTED_RESTORE", flush=True)
            output = compose("run", "--rm", "drill", timeout=600)
            evidence = [line.removeprefix("BACKUP_EVIDENCE ") for line in output.splitlines()
                        if line.startswith("BACKUP_EVIDENCE ")]
            if len(evidence) != 1:
                raise ValueError
            report = json.loads(evidence[0])
            if report["head"] != head or report["offsite"] != "BLOCKED" or report["target_rpo_rto"] != "BLOCKED":
                raise ValueError
            if args.report:
                args.report.write_text(json.dumps(report, indent=2, sort_keys=True)+"\n")
            print("BACKUP_EVIDENCE " + json.dumps(report, sort_keys=True), flush=True)
            return 0
        except Exception as error:
            # Private diagnostics stay local; never upload child logs as artifacts.
            debug = Path(tempfile.mkdtemp(prefix="akbriz-backup-failure-")) / "private.log"
            debug.write_text((getattr(error, "stdout", "") or "") + (getattr(error, "stderr", "") or "") + str(type(error).__name__))
            debug.chmod(0o600)
            if claimed:
                try:
                    with debug.open("a") as stream:
                        stream.write(compose("logs", "--no-color", "--tail=40"))
                except Exception:
                    pass
            print("BACKUP_REHEARSAL_BLOCKED", file=sys.stderr)
            return 1
        finally:
            if claimed:
                compose("down")  # No -v, no pruning, no production resources.


if __name__ == "__main__":
    sys.exit(main())
