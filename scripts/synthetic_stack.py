"""Fail closed before starting, seeding or deleting an HTTPS acceptance stack."""

import json
import re
import subprocess

LABEL = "com.akbrizcase.synthetic-acceptance"
MARKER = "akbrizcase:synthetic-seed:demo"


class SyntheticStack:
    def __init__(self, project, compose, environment):
        if not re.fullmatch(r"akbrizcase-https-test-[0-9a-f]{10}", project):
            raise RuntimeError("Refusing a non-acceptance project")
        self.project = project
        self.compose = compose
        self.environment = environment
        self.names = {f"{project}_{name}" for name in ("postgres_data", "app_files")}
        self.claimed = False

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
        if set(volumes) != {"postgres_data", "app_files"} or any(
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
            for v in config["services"]["postgres"]["volumes"]
            if v["target"] == "/var/lib/postgresql/data"
        ]
        if (
            len(mounts) != 1
            or mounts[0].get("type") != "volume"
            or mounts[0].get("source") != "postgres_data"
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
            container = self.compose("ps", "--all", "--quiet", "postgres").strip()
            detail = json.loads(self.docker("inspect", container))[0]
            mounts = [
                m
                for m in detail["Mounts"]
                if m["Destination"] == "/var/lib/postgresql/data"
            ]
            if (
                len(mounts) != 1
                or mounts[0].get("Type") != "volume"
                or mounts[0].get("Name") != f"{self.project}_postgres_data"
            ):
                raise RuntimeError("PostgreSQL is not mounted on our fresh volume")

    def sql(self, statement):
        return self.compose(
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
        ).strip()

    def verify_database(self):
        self.verify_volumes()
        if (
            self.sql(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                "WHERE datname=current_database()"
            )
            != MARKER
        ):
            raise RuntimeError("Refusing an unmarked synthetic database")

    def cleanup(self):
        if self.claimed:
            self.verify_volumes(complete=False)
            # Only the claimed, labelled disposable project; never user volumes.
            self.compose("down", "--volumes")
