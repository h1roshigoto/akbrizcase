"""Fail-closed, local synthetic staging controller. No remote deploy capability.

All subprocess diagnostics are captured and discarded: only closed stage codes
are printed. Manifests contain image identities, never runtime configuration.
"""

import argparse
from email.message import Message
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

REPO = Path(__file__).resolve().parents[1]
SCHEMA = "0011_mvp039_http_security"
SECRETS = (
    "postgres_password",
    "migrator_password",
    "runtime_password",
    "staff_csrf_secret",
    "order_hmac_secret",
    "ip_hmac_secret",
)
MODES = {
    "format": 1,
    "data": "synthetic-only",
    "catalog": True,
    "orders": False,
    "chat": False,
    "knowledge": False,
    "files": False,
    "smtp": "disabled",
    "clamav": "disabled-files-blocked",
    "signature_update": "disabled-files-blocked",
    "real_pii": False,
}
FULL_MODES = {
    "format": 2,
    "profile": "full-synthetic-staging",
    "data": "synthetic-only",
    "catalog": True,
    "orders": True,
    "chat": True,
    "knowledge": True,
    "files": True,
    "smtp": "isolated-sink",
    "clamav": "required",
    "signature_update": "operator-verified-offline",
    "real_pii": False,
}
REDUCED_PROFILE = "reduced-synthetic-rehearsal"
FULL_PROFILE = "full-synthetic-staging"
IMAGE = re.compile(r"(?:sha256:[0-9a-f]{64}|[a-z0-9./:_-]+@sha256:[0-9a-f]{64})")


class Blocked(Exception):
    pass


def require(condition: bool) -> None:
    if not condition:
        raise Blocked


def run(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 180,
) -> str:
    result = subprocess.run(
        args,
        cwd=REPO,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        for code in (
            "MIGRATION_AUTH_FAILED",
            "MIGRATION_PRIVILEGE_FAILED",
            "MIGRATION_SCHEMA_FAILED",
            "MIGRATION_CONSTRAINT_FAILED",
            "MIGRATION_DATABASE_FAILED",
            "MIGRATION_FILE_FAILED",
            "MIGRATION_CONFIGURATION_FAILED",
        ):
            if code in result.stderr.splitlines():
                print("RELEASE_" + code, flush=True)
        raise Blocked
    return result.stdout.strip()


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_json(path: Path) -> dict:
    def unique(pairs: list[tuple[str, object]]) -> dict:
        output: dict = {}
        for key, value in pairs:
            require(key not in output)
            output[key] = value
        return output

    require(path.stat().st_size <= 65536)
    value = json.loads(path.read_text(), object_pairs_hook=unique)
    require(isinstance(value, dict))
    return value


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def protected(path: Path, *, readable: bool = False) -> None:
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not path.is_symlink())
    require(info.st_uid == os.getuid())
    require(info.st_size > 0 and not info.st_mode & 0o022)
    require(readable or not info.st_mode & 0o077)


def manifest_valid(manifest: dict) -> None:
    require(
        set(manifest)
        == {"format", "commit", "schema", "backend", "frontend", "postgres"}
    )
    require(
        type(manifest["format"]) is int
        and manifest["format"] == 1
        and manifest["schema"] == SCHEMA
    )
    require(re.fullmatch(r"[0-9a-f]{40}", manifest["commit"]) is not None)
    for name in ("backend", "frontend", "postgres"):
        require(
            isinstance(manifest[name], str)
            and IMAGE.fullmatch(manifest[name]) is not None
        )


class Release:
    def __init__(self, root: Path, manifest: dict):
        self.root = root
        self.manifest = manifest
        self.stage = "PREFLIGHT"
        self.stopped = False
        self.config: dict = {}
        self.env: dict[str, str] = {}
        self.modes = MODES.copy()
        self.full = False
        self.fresh_database = False

    @property
    def profile(self) -> str:
        return FULL_PROFILE if self.full else REDUCED_PROFILE

    def compose_files(self) -> list[str]:
        files = ["-f", str(REPO / "compose.release.yml")]
        if self.full:
            files += ["-f", str(REPO / "compose.release-full.yml")]
            if self.config["av"] == "synthetic-fixture":
                files += ["-f", str(REPO / "compose.release-fixture.yml")]
        return files

    def receipt(self) -> dict:
        return {
            "format": 2,
            "profile": self.profile,
            "project": self.config["project"],
            "av": self.config["av"] if self.full else "disabled-files-blocked",
            "modes": fingerprint(self.modes),
            "manifest": self.manifest,
            "fingerprint": fingerprint(self.manifest),
        }

    def accepted(self, history: dict) -> bool:
        require(set(history) == {"releases"} and type(history["releases"]) is list)
        found = False
        for record in history["releases"]:
            require(isinstance(record, dict))
            legacy = set(record) == {"manifest", "fingerprint"}
            if legacy:
                # Format-1 receipts can only mean the original reduced profile.
                manifest_valid(record["manifest"])
                require(record["fingerprint"] == fingerprint(record["manifest"]))
                if not self.full and record["manifest"] == self.manifest:
                    found = True
            else:
                require(
                    set(record)
                    == {
                        "format",
                        "profile",
                        "project",
                        "av",
                        "modes",
                        "manifest",
                        "fingerprint",
                    }
                )
                require(type(record["format"]) is int and record["format"] == 2)
                require(record["profile"] in {FULL_PROFILE, REDUCED_PROFILE})
                require(
                    isinstance(record["project"], str)
                    and re.fullmatch(r"akbriz-staging-[0-9a-f]{12}", record["project"])
                    is not None
                )
                require(
                    record["av"]
                    in (
                        {"synthetic-fixture", "signed-cvd"}
                        if record["profile"] == FULL_PROFILE
                        else {"disabled-files-blocked"}
                    )
                )
                expected_modes = (
                    FULL_MODES if record["profile"] == FULL_PROFILE else MODES
                )
                require(record["modes"] == fingerprint(expected_modes))
                manifest_valid(record["manifest"])
                require(record["fingerprint"] == fingerprint(record["manifest"]))
                if record == self.receipt():
                    found = True
        return found

    def event(self, stage: str) -> None:
        self.stage = stage
        print("RELEASE_" + stage, flush=True)

    def compose_command(self, *args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "--project-directory",
            str(REPO),
            *self.compose_files(),
            "-p",
            self.config["project"],
            *args,
        ]

    def compose(self, *args: str, input_text: str | None = None) -> str:
        return run(
            self.compose_command(*args),
            env=self.env,
            input_text=input_text,
        )

    def preflight(self) -> None:
        require(self.root.is_absolute() and self.root == self.root.resolve())
        require(not self.root.is_relative_to(REPO))
        require(stat.S_IMODE(self.root.stat().st_mode) == 0o700)
        require(self.root.stat().st_uid == os.getuid())
        protected(self.root / "staging.json")
        self.config = read_json(self.root / "staging.json")
        self.full = self.config.get("profile") == FULL_PROFILE
        require(
            set(self.config)
            == (
                {"project", "port", "scope", "profile", "av"}
                if self.full
                else {"project", "port", "scope"}
            )
        )
        if self.full:
            require(self.config["av"] in {"synthetic-fixture", "signed-cvd"})
        self.modes = FULL_MODES.copy() if self.full else MODES.copy()
        require(self.config["scope"] == "local-synthetic-rehearsal")
        require(
            re.fullmatch(r"akbriz-staging-[0-9a-f]{12}", self.config["project"])
            is not None
        )
        require(
            type(self.config["port"]) is int and 1024 <= self.config["port"] <= 65535
        )
        manifest_valid(self.manifest)
        # Require the commit to exist locally; no floating branch/tag identity.
        require(
            run(["git", "rev-parse", self.manifest["commit"] + "^{commit}"])
            == self.manifest["commit"]
        )
        for name in (*SECRETS, "server.key", "server.crt", "ca.crt", "modes.json"):
            protected(self.root / name, readable=True)
        values = [(self.root / name).read_bytes().strip() for name in SECRETS]
        require(all(32 <= len(value) <= 4096 for value in values))
        require(len(set(values)) == len(values))
        modes = read_json(self.root / "modes.json")
        require(canonical(modes) == canonical(self.modes))
        # Check bind-mounted trusted fixture sources against this exact candidate.
        paths = [
            "compose.release.yml",
            "infrastructure/release/nginx.conf",
            "infrastructure/postgres/provision.sh",
        ]
        if self.full:
            paths += [
                "compose.release-full.yml",
                "backend/tests/integration/smtp_double.py",
            ]
            if self.config["av"] == "synthetic-fixture":
                paths += [
                    "compose.release-fixture.yml",
                    "backend/tests/browser_media_server.py",
                    "scripts/worker_probe.py",
                    "scripts/notification_retry_probe.py",
                ]
                signatures = self.root / "signatures"
                require(not signatures.is_symlink() and signatures.is_dir())
                require(stat.S_IMODE(signatures.stat().st_mode) == 0o555)
                signature = signatures / "test.ndb"
                protected(signature, readable=True)
                require(set(p.name for p in signatures.iterdir()) == {"test.ndb"})
                require(stat.S_IMODE(signature.stat().st_mode) == 0o444)
                require(
                    signature.read_bytes()
                    == b"Synthetic.Browser.Test:0:*:53594e5448494e464543544544\n"
                )
            else:
                signatures = self.root / "signatures"
                require(not signatures.is_symlink() and signatures.is_dir())
                require(stat.S_IMODE(signatures.stat().st_mode) == 0o555)
                require(
                    set(p.name for p in signatures.iterdir())
                    == {"main.cvd", "daily.cvd", "bytecode.cvd"}
                )
                for signature in signatures.iterdir():
                    protected(signature, readable=True)
                    require(stat.S_IMODE(signature.stat().st_mode) == 0o444)
        for path in paths:
            expected = run(["git", "show", self.manifest["commit"] + ":" + path])
            require((REPO / path).read_text().strip() == expected)
        # TLS is trusted and hostname checked, but explicitly rehearsal-only.
        run(
            [
                "openssl",
                "verify",
                "-CAfile",
                str(self.root / "ca.crt"),
                "-verify_hostname",
                "localhost",
                str(self.root / "server.crt"),
            ]
        )
        run(
            [
                "openssl",
                "x509",
                "-in",
                str(self.root / "server.crt"),
                "-checkend",
                "86400",
                "-noout",
            ]
        )
        certkey = run(
            [
                "openssl",
                "x509",
                "-in",
                str(self.root / "server.crt"),
                "-pubkey",
                "-noout",
            ]
        )
        privatekey = run(
            ["openssl", "pkey", "-in", str(self.root / "server.key"), "-pubout"]
        )
        require(certkey == privatekey)
        # Strip ambient Compose/project/DB/SMTP settings and daemon redirection.
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(
                (
                    "COMPOSE_",
                    "RELEASE_",
                    "DB_",
                    "SMTP_",
                    "POSTGRES_",
                    "DOCKER_HOST",
                    "DOCKER_CONTEXT",
                    "DOCKER_TLS",
                    "DOCKER_CERT_PATH",
                )
            )
        }
        self.env.update(
            RELEASE_PROJECT=self.config["project"],
            RELEASE_ROOT=str(self.root),
            RELEASE_PORT=str(self.config["port"]),
            RELEASE_BACKEND=self.manifest["backend"],
            RELEASE_FRONTEND=self.manifest["frontend"],
            RELEASE_POSTGRES=self.manifest["postgres"],
        )
        self.env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        # Refuse unrelated containers or volumes already using the selected project.
        project = self.config["project"]
        claim = self.config | ({"modes": fingerprint(self.modes)} if self.full else {})
        if (self.root / "claim.json").exists():
            protected(self.root / "claim.json")
            require(read_json(self.root / "claim.json") == claim)
        for volume in (project + "_database", project + "_files"):
            result = subprocess.run(
                ["docker", "volume", "inspect", volume],
                env=self.env,
                text=True,
                capture_output=True,
                timeout=15,
            )
            if result.returncode == 0:
                require(read_json(self.root / "claim.json") == claim)
                labels = json.loads(result.stdout)[0].get("Labels") or {}
                require(labels.get("com.akbrizcase.synthetic-release") == "true")
                require(labels.get("com.docker.compose.project") == project)
            else:
                require("no such volume" in result.stderr.lower())
                if volume == project + "_database":
                    self.fresh_database = True
        containers = run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ],
            env=self.env,
        ).split()
        if containers:
            # The durable claim ties this namespace to this exact private root.
            require(read_json(self.root / "claim.json") == claim)
        if not (self.root / "claim.json").exists():
            write_json(self.root / "claim.json", claim)
        if self.full:
            authority = {
                "format": 1,
                "scope": self.config["scope"],
                "project": project,
                "profile": self.profile,
                "modes": fingerprint(self.modes),
            }
            authority_path = self.root / "authority.json"
            if authority_path.exists():
                require(read_json(authority_path) == authority)
            else:
                write_json(authority_path, authority)
                authority_path.chmod(0o444)

    def images(self) -> None:
        for name in ("backend", "frontend", "postgres"):
            ref = self.manifest[name]
            info = json.loads(run(["docker", "image", "inspect", ref], env=self.env))[0]
            require(
                info["Id"] == ref
                if ref.startswith("sha256:")
                else ref in info.get("RepoDigests", [])
            )
            labels = info["Config"].get("Labels") or {}
            if name != "postgres":
                require(
                    labels.get("org.opencontainers.image.revision")
                    == self.manifest["commit"]
                )
                require(labels.get("com.akbrizcase.release-gates") == "1")
                if self.full:
                    require(labels.get("com.akbrizcase.release-profiles") == "2")
            else:
                require("PG_MAJOR=16" in info["Config"].get("Env", []))
        actual = run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                self.manifest["backend"],
                "python",
                "-c",
                "from app.core.db import SCHEMA_REVISION; print(SCHEMA_REVISION)",
            ],
            env=self.env,
        )
        require(actual == self.manifest["schema"])
        rendered = json.loads(self.compose("config", "--format", "json"))
        # YAML flow lists require quoting comma-bearing tmpfs specifications.
        # Compose syntax validation alone accepts the resulting invalid mounts.
        for service in rendered["services"].values():
            require(all(mount.startswith("/") for mount in service.get("tmpfs", [])))

    def sql(self, query: str) -> str:
        return self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-U",
            "akbrizcase_owner",
            "-d",
            "akbrizcase",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            query,
        )

    def http(self, path: str, *, method: str = "GET") -> tuple[int, bytes, Message]:
        context = ssl.create_default_context(cafile=str(self.root / "ca.crt"))
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
        request = Request(
            f"https://localhost:{self.config['port']}{path}", method=method
        )
        try:
            response = opener.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read(), response.headers

    def smoke(self) -> None:
        self.event("SMOKE_TLS")
        require(self.http("/readyz")[:2] == (200, b'{"status":"ok"}'))
        require(self.http("/")[0] == 200)
        self.event("SMOKE_MODES")
        disabled = (
            ()
            if self.full
            else (
                ("/api/v1/orders", "POST"),
                ("/api/v1/chat/sessions", "POST"),
                ("/api/v1/documents", "GET"),
                ("/api/v1/knowledge/search", "GET"),
            )
        )
        for path, method in disabled:
            status, body, headers = self.http(path, method=method)
            require(
                status == 503
                and json.loads(body)["error"]["code"] == "RELEASE_MODE_DISABLED"
            )
            require(headers.get("Cache-Control") == "no-store")
        if self.full:
            for path in (
                "/api/v1/catalog/items",
                "/api/v1/chat/scenarios?model_id=00000000-0000-4000-8000-000000000000",
                "/api/v1/knowledge/search?q=synthetic",
            ):
                require(self.http(path)[0] == 200)
        self.event("SMOKE_FILES")
        for path in (
            "/files/private",
            "/documents/private",
            "/images/private",
            "/.env",
        ):
            require(self.http(path)[0] == 404)
        self.event("SMOKE_BOUNDARIES")
        for service in (
            "api",
            "worker",
            "postgres",
            "nginx",
            *(("smtp-test",) if self.full else ()),
        ):
            ids = self.compose("ps", "-q", service).split()
            require(len(ids) == 1)
            info = json.loads(run(["docker", "inspect", ids[0]], env=self.env))[0]
            require(
                all(
                    "docker.sock" not in mount["Destination"]
                    for mount in info["Mounts"]
                )
            )
            if self.full:
                topology = {
                    "api": {"web", "data", "mail"},
                    "worker": {"data", "mail"},
                    "postgres": {"data"},
                    "nginx": {"ingress", "web"},
                    "smtp-test": {"mail"},
                }
                expected_networks = {
                    self.config["project"] + "_" + n for n in topology[service]
                }
                require(set(info["NetworkSettings"]["Networks"]) == expected_networks)
                for network in topology[service]:
                    actual = json.loads(
                        run(
                            [
                                "docker",
                                "network",
                                "inspect",
                                self.config["project"] + "_" + network,
                            ],
                            env=self.env,
                        )
                    )[0]
                    require(actual["Internal"] == (network != "ingress"))
                    require(
                        actual["Labels"].get("com.docker.compose.project")
                        == self.config["project"]
                    )
                host = info["HostConfig"]
                require(host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"])
                require("no-new-privileges:true" in host["SecurityOpt"])
                require(not info["State"].get("OOMKilled"))
                if service in {"api", "worker", "smtp-test"}:
                    require(info["Config"]["User"] == "10001:10001")
            bindings = info["HostConfig"].get("PortBindings") or {}
            if service == "nginx":
                require(
                    bindings
                    == {
                        "8443/tcp": [
                            {
                                "HostIp": "127.0.0.1",
                                "HostPort": str(self.config["port"]),
                            }
                        ]
                    }
                )
                require(
                    all(
                        "/var/lib/" not in mount["Destination"]
                        and "/run/secrets" not in mount["Destination"]
                        for mount in info["Mounts"]
                    )
                )
            else:
                require(not bindings)
            expected = {
                self.config["project"] + suffix for suffix in ("_files", "_database")
            }
            require(
                all(
                    mount["Name"] in expected
                    for mount in info["Mounts"]
                    if mount["Type"] == "volume"
                )
            )

    def apply(self, *, rollback: bool = False) -> None:
        history_path = self.root / "accepted.json"
        history = read_json(history_path) if history_path.exists() else {"releases": []}
        receipt = self.receipt()
        previously_accepted = self.accepted(history)
        if rollback:
            require(previously_accepted)
        self.event("IMAGES")
        self.images()
        self.event("DATABASE")
        self.compose(
            "up", "--no-build", "-d", "--wait", "--wait-timeout", "90", "postgres"
        )
        require(self.sql("SELECT current_database()") == "akbrizcase")
        self.event("QUIESCE")
        self.stopped = True
        self.compose("stop", "nginx", "api", "worker")
        # Rollback never migrates. Unconfirmed schema compatibility is a manual gate.
        if rollback:
            self.event("ROLLBACK_COMPATIBILITY")
            require(
                self.sql("SELECT version_num FROM app.alembic_version")
                == self.manifest["schema"]
            )
        else:
            self.event("MIGRATION")
            self.compose("run", "--rm", "--no-deps", "migrate")
        if self.full:
            self.event("SYNTHETIC_DATABASE")
            marker = self.sql(
                "SELECT coalesce(shobj_description(oid, 'pg_database'), '') FROM pg_database WHERE datname=current_database()"
            )
            if self.fresh_database and not marker and not rollback:
                require(self.sql("SELECT count(*) FROM app.orders") == "0")
                require(self.sql("SELECT count(*) FROM app.staff_users") == "0")
                self.sql(
                    "COMMENT ON DATABASE akbrizcase IS 'akbrizcase:synthetic-seed:demo'"
                )
            else:
                require(marker == "akbrizcase:synthetic-seed:demo")
            self.event("DEPENDENCIES")
            self.compose("up", "--no-build", "-d", "--wait", "smtp-test")
        self.event("READINESS")
        self.compose("run", "--rm", "--no-deps", "probe")
        self.event("START")
        self.compose(
            "up",
            "--no-build",
            "-d",
            "--wait",
            "--wait-timeout",
            "90",
            "api",
            "worker",
            "nginx",
        )
        self.event("SMOKE")
        self.smoke()
        if receipt not in history["releases"]:
            history["releases"].append(receipt)
        write_json(history_path, history)
        write_json(
            self.root / "status.json",
            {
                "status": "accepted-local-rehearsal",
                "manifest": fingerprint(self.manifest),
                "profile": self.profile,
                "modes": fingerprint(self.modes),
                "real_staging": "BLOCKED",
            },
        )
        self.event("ACCEPTED_LOCAL_REHEARSAL")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply", "rollback"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    release = None
    lock = None
    try:
        # Lock before reading/writing state, for the entire release operation.
        require(args.root.is_absolute() and args.root == args.root.resolve())
        require(stat.S_IMODE(args.root.stat().st_mode) == 0o700)
        require(
            not args.root.is_relative_to(REPO)
            and args.root.stat().st_uid == os.getuid()
        )
        descriptor = os.open(
            args.root / "release.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        lock = os.fdopen(descriptor, "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release = Release(args.root, {})
        release.manifest = read_json(args.manifest)
        release.preflight()
        if args.action == "check":
            release.images()
            print("RELEASE_CHECKED_LOCAL_REHEARSAL")
        else:
            write_json(
                args.root / "status.json",
                {"status": "in-progress", "real_staging": "BLOCKED"},
            )
            release.apply(rollback=args.action == "rollback")
        return 0
    except Exception:
        if release is not None and release.stopped:
            try:
                release.compose("stop", "nginx", "api", "worker")
            except Exception:
                pass
        stage = release.stage if release is not None else "PREFLIGHT"
        # A preflight failure also invalidates a stale successful status.
        try:
            if release is not None and args.action != "check":
                write_json(
                    args.root / "status.json",
                    {"status": "blocked", "stage": stage, "real_staging": "BLOCKED"},
                )
        except Exception:
            pass
        print("RELEASE_BLOCKED_" + stage, file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
