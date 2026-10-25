"""Fail closed before starting, seeding or deleting an HTTPS acceptance stack."""

import json
import re
import secrets
import subprocess
from pathlib import Path

LABEL = "com.akbrizcase.synthetic-acceptance"
MARKER = "akbrizcase:synthetic-seed:demo"


def isolated_environment(environment):
    environment = environment.copy()
    for name in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES"):
        environment.pop(name, None)
    return environment


def synthetic_secrets(directory, environment):
    for name, variable in (
        ("postgres_password", "POSTGRES_PASSWORD_FILE"),
        ("migrator_password", "MIGRATOR_PASSWORD_FILE"),
        ("runtime_password", "RUNTIME_PASSWORD_FILE"),
        ("staff_csrf_secret", "STAFF_CSRF_SECRET_FILE"),
        ("order_hmac_secret", "ORDER_HMAC_SECRET_FILE"),
    ):
        path = Path(directory) / name
        path.write_text(secrets.token_urlsafe(48) + "\n")
        path.chmod(0o444)
        environment[variable] = str(path)


class SyntheticStack:
    def __init__(self, project, compose, environment):
        if not re.fullmatch(
            r"akbrizcase-(https-test-[0-9a-f]{10}|smoke-[0-9a-f]{10}|"
            r"catalog-test-[0-9a-f]{12}|dbtest-[0-9a-f]{12})",
            project,
        ):
            raise RuntimeError("Refusing a non-acceptance project")
        self.project = project
        self.compose = compose
        self.environment = environment
        integration = project.startswith("akbrizcase-dbtest-")
        self.volume_keys = (
            {"test_data"} if integration else {"postgres_data", "app_files"}
        )
        self.db_volume = "test_data" if integration else "postgres_data"
        self.db_service = "postgres-test" if integration else "postgres"
        self.marker = "akbrizcase:synthetic-seed:test" if integration else MARKER
        self.names = {f"{project}_{name}" for name in self.volume_keys}
        self.claimed = False

    def write_override(self, directory):
        path = Path(directory) / "acceptance.json"
        path.write_text(
            json.dumps(
                {
                    "volumes": {
                        name: {"labels": {LABEL: self.project}}
                        for name in self.volume_keys
                    }
                }
            )
        )
        return path

    def docker(self, *args):
        return subprocess.run(
            ["docker", *args],
            env=self.environment,
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout.strip()

    def volumes(self):
        return self.docker("volume", "ls", "-q").splitlines()

    def claim(self):
        config = json.loads(self.compose("config", "--format", "json"))
        volumes = config["volumes"]
        if set(volumes) != self.volume_keys or any(
            v.get("external")
            or v.get("driver_opts")
            or v.get("driver") not in (None, "local")
            or v.get("name") != f"{self.project}_{key}"
            or v.get("labels", {}).get(LABEL) != self.project
            for key, v in volumes.items()
        ):
            raise RuntimeError("Refusing external, shared or unlabelled volumes")
        mounts = [
            v
            for v in config["services"][self.db_service]["volumes"]
            if v["target"] == "/var/lib/postgresql/data"
        ]
        if (
            len(mounts) != 1
            or mounts[0].get("type") != "volume"
            or mounts[0].get("source") != self.db_volume
        ):
            raise RuntimeError("Refusing a shared PostgreSQL mount before startup")
        if self.names.intersection(self.volumes()) or self.docker(
            "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}"
        ):
            raise RuntimeError("Refusing a pre-existing acceptance stack")
        self.claimed = True

    def verify_volumes(self, *, complete=True):
        if not self.claimed:
            raise RuntimeError("Acceptance stack was not claimed")
        existing = set(self.volumes())
        owned = set(
            self.docker(
                "volume",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ).splitlines()
        )
        if owned - self.names or (complete and not self.names <= existing):
            raise RuntimeError("Unexpected acceptance volumes; preserve for inspection")
        for name in self.names & existing:
            volume = json.loads(self.docker("volume", "inspect", name))[0]
            labels = volume.get("Labels") or {}
            if (
                labels.get(LABEL) != self.project
                or labels.get("com.docker.compose.project") != self.project
            ):
                raise RuntimeError("Volume ownership mismatch; preserve for inspection")
        if complete:
            container = self.compose("ps", "--all", "--quiet", self.db_service).strip()
            detail = json.loads(self.docker("inspect", container))[0]
            mounts = [
                m
                for m in detail["Mounts"]
                if m["Destination"] == "/var/lib/postgresql/data"
            ]
            if (
                len(mounts) != 1
                or mounts[0].get("Type") != "volume"
                or mounts[0].get("Name") != f"{self.project}_{self.db_volume}"
            ):
                raise RuntimeError("PostgreSQL is not mounted on our fresh volume")

    def sql(self, statement):
        return self.compose(
            "exec",
            "-T",
            self.db_service,
            "psql",
            "-U",
            "akbrizcase_owner",
            "-d",
            "akbrizcase",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            statement,
        ).strip()

    def verify_database(self):
        self.verify_volumes()
        if (
            self.sql(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                "WHERE datname=current_database()"
            )
            != self.marker
        ):
            raise RuntimeError("Refusing an unmarked synthetic database")

    def cleanup(self):
        if self.claimed:
            self.verify_volumes(complete=False)
            retained = self.names.intersection(self.volumes())
            # Containers/networks only. No acceptance command may delete volumes.
            self.compose("down")
            if not retained <= set(self.volumes()):
                raise RuntimeError("Acceptance volumes disappeared during shutdown")
            self.verify_volumes(complete=False)
            print(
                "Retained synthetic volumes: " + ", ".join(sorted(retained)), flush=True
            )

    def recover_for_stop(self):
        """Adopt only labelled integration resources, solely for non-volume down."""
        if not self.project.startswith("akbrizcase-dbtest-"):
            raise RuntimeError("Only integration recovery is supported")
        identifiers = self.docker(
            "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}"
        ).splitlines()
        for identifier in identifiers:
            detail = json.loads(self.docker("inspect", identifier))[0]
            labels = detail["Config"]["Labels"]
            if (
                labels.get("com.akbrizcase.db-integration") != "true"
                or labels.get("com.docker.compose.project") != self.project
            ):
                raise RuntimeError("Refusing to stop an unowned integration container")
        self.claimed = True
        try:
            self.verify_volumes(complete=False)
        except Exception:
            self.claimed = False
            raise
