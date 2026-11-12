"""Build exact-commit images and exercise disposable synthetic release/rollback.

Keeps volumes and private runtime material for inspection; never removes data.
Only a newly claimed random local Compose namespace can be exercised.
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import uuid

from release import (
    MODES,
    FULL_MODES,
    FULL_PROFILE,
    REDUCED_PROFILE,
    REPO,
    SCHEMA,
    SECRETS,
    Release,
    read_json,
    require,
    run,
    write_json,
)


def prepare(root: Path, *, profile: str = REDUCED_PROFILE) -> None:
    require(not any(root.iterdir()))
    root.chmod(0o700)
    for name in SECRETS:
        (root / name).write_text(secrets.token_urlsafe(48))
        (root / name).chmod(0o444)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    write_json(
        root / "staging.json",
        {
            "project": "akbriz-staging-" + uuid.uuid4().hex[:12],
            "port": port,
            "scope": "local-synthetic-rehearsal",
            **(
                {"profile": FULL_PROFILE, "av": "synthetic-fixture"}
                if profile == FULL_PROFILE
                else {}
            ),
        },
    )
    if profile == FULL_PROFILE:
        signatures = root / "signatures"
        signatures.mkdir(mode=0o700)
        (signatures / "test.ndb").write_text(
            "Synthetic.Browser.Test:0:*:53594e5448494e464543544544\n"
        )
        (signatures / "test.ndb").chmod(0o444)
        signatures.chmod(0o555)
    write_json(root / "modes.json", FULL_MODES if profile == FULL_PROFILE else MODES)
    (root / "modes.json").chmod(0o444)
    run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "3",
            "-subj",
            "/CN=LOCAL SYNTHETIC REHEARSAL ONLY",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            str(root / "server.key"),
            "-out",
            str(root / "server.crt"),
        ]
    )
    (root / "server.key").chmod(0o444)
    (root / "ca.crt").write_bytes((root / "server.crt").read_bytes())
    (root / "ca.crt").chmod(0o444)
    (root / "server.crt").chmod(0o444)


def build(root: Path) -> tuple[dict, dict]:
    require(not run(["git", "status", "--porcelain"]))
    commit = run(["git", "rev-parse", "HEAD"])
    common = [
        "--label",
        "org.opencontainers.image.revision=" + commit,
        "--label",
        "com.akbrizcase.release-gates=1",
        "--label",
        "com.akbrizcase.release-profiles=2",
    ]
    if os.environ.get("CODEX_PROXY_CERT"):
        common += ["--secret", "id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt"]
    for name, context in (("backend", "backend"), ("frontend", "frontend")):
        print("REHEARSAL_BUILD_" + name.upper(), flush=True)
        run(
            [
                "docker",
                "build",
                *common,
                "--iidfile",
                str(root / (name + ".id")),
                "-f",
                str(REPO / context / "Dockerfile"),
                str(REPO / context),
            ],
            timeout=1200,
        )
    print("REHEARSAL_RESOLVE_POSTGRES", flush=True)
    run(["docker", "pull", "postgres:16.13-bookworm"], timeout=180)
    pg = json.loads(run(["docker", "image", "inspect", "postgres:16.13-bookworm"]))[0][
        "RepoDigests"
    ][0]
    first = {
        "format": 1,
        "commit": commit,
        "schema": SCHEMA,
        "postgres": pg,
        **{
            name: (root / (name + ".id")).read_text().strip()
            for name in ("backend", "frontend")
        },
    }
    # Two immutable packaging versions of the SAME source commit. This proves
    # application-image replacement; it does not claim cross-schema compatibility.
    print("REHEARSAL_BUILD_CANDIDATE", flush=True)
    run(
        [
            "docker",
            "build",
            *common,
            "--label",
            "com.akbrizcase.rehearsal-generation=2",
            "--iidfile",
            str(root / "candidate.id"),
            "-f",
            str(REPO / "backend/Dockerfile"),
            str(REPO / "backend"),
        ],
        timeout=1200,
    )
    second = first | {"backend": (root / "candidate.id").read_text().strip()}
    write_json(root / "first.json", first)
    write_json(root / "candidate.json", second)
    print(
        "REHEARSAL_IDENTITY "
        + json.dumps({"first": first, "candidate": second}, sort_keys=True),
        flush=True,
    )
    return first, second


def invoke(
    root: Path, manifest: Path, *, rollback: bool = False, success: bool = True
) -> str:
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "scripts/release.py",
            "rollback" if rollback else "apply",
            "--root",
            str(root),
            "--manifest",
            str(manifest),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=400,
    )
    output = result.stdout + result.stderr
    # Closed output: no subprocess output, path, DSN or arbitrary exception.
    require(
        all(
            line.startswith("RELEASE_") and line.replace("_", "").isalnum()
            for line in output.splitlines()
        )
    )
    print(output.strip(), flush=True)
    require((result.returncode == 0) == success)
    status = read_json(root / "status.json")
    require(status["status"] == ("accepted-local-rehearsal" if success else "blocked"))
    return output


def fixture(release: Release) -> None:
    release.sql("COMMENT ON DATABASE akbrizcase IS 'akbrizcase:synthetic-seed:demo'")
    code = """
import os, secrets, uuid
from pathlib import Path
from sqlalchemy import select
from app.core.db import Database, DatabaseSettings
from app.modules.catalog.seed import seed_catalog
from app.modules.catalog.models import CatalogItem
from app.modules.orders.seed import seed_consent
from app.modules.orders.service import create_order
from app.modules.orders.security import OrderSecurity
from app.contracts.canonical import parse_order_json
from app.schemas.orders import OrderCreate
from app.schemas.common import OrderHeaders
import json
os.environ['APP_ENV'] = 'demo'
db = Database(DatabaseSettings.from_environment())
seed_catalog(db, mode='demo')
seed_consent(db, mode='demo')
with db.unit_of_work() as session:
    item = session.scalar(select(CatalogItem).where(CatalogItem.sku == 'DEMO-P-001'))
    body = {'kind':'commercial', 'items':[{'item_id':str(item.id), 'qty':2, 'expected_revision':item.revision}],
            'contact':{'name':'Synthetic Rehearsal','email':'release@example.invalid'},
            'source':'site', 'confirmed':True,
            'consent':{'version':'SYNTHETIC-DEMO-ONLY-v1','accepted':True}}
    request = OrderCreate.model_validate(parse_order_json(json.dumps(body).encode()))
    headers = OrderHeaders.model_validate({'Idempotency-Key':str(uuid.uuid4()),'X-Request-Token':secrets.token_hex(32)})
    create_order(session, request, headers, OrderSecurity.from_environment())
Path('/var/lib/akbrizcase/files/synthetic-release-sentinel').write_bytes(b'SYNTHETIC RELEASE FILE ONLY')
db.close()
"""
    release.compose("exec", "-T", "api", "python", "-", input_text=code)


def snapshot(release: Release) -> str:
    # Compare complete retained rows without exporting their contents or hashes.
    # Worker bookkeeping may advance, so only persistence subjects are selected.
    query = (
        "SELECT md5(concat_ws('|',"
        + ",".join(
            f"(SELECT string_agg(row_to_json(t)::text, ',' ORDER BY id) FROM app.{table} t)"
            for table in (
                "orders",
                "order_items",
                "order_status_history",
                "email_outbox",
                "consents",
            )
        )
        + "))"
    )
    counts = release.sql(
        "SELECT (SELECT count(*) FROM app.orders), (SELECT count(*) FROM app.order_items), (SELECT count(*) FROM app.email_outbox)"
    )
    require(counts == "1|1|1")
    file = release.compose(
        "exec",
        "-T",
        "api",
        "python",
        "-c",
        "from pathlib import Path; assert Path('/var/lib/akbrizcase/files/synthetic-release-sentinel').read_bytes() == b'SYNTHETIC RELEASE FILE ONLY'",
    )
    require(file == "")
    return release.sql(query)


def verify_leaks(
    release: Release, root: Path, first: dict, second: dict, *, extra=()
) -> None:
    logs = release.compose(
        "logs",
        "--no-color",
        "--no-log-prefix",
        "api",
        "worker",
        "nginx",
        "postgres",
    )
    for secret in extra:
        require(secret not in logs)
    for name in SECRETS:
        require((root / name).read_text().strip() not in logs)
    for image in (first["backend"], first["frontend"], second["backend"]):
        metadata = run(["docker", "image", "inspect", image], env=release.env)
        history = run(
            [
                "docker",
                "history",
                "--no-trunc",
                "--format",
                "{{.CreatedBy}}",
                image,
            ],
            env=release.env,
        )
        for name in SECRETS:
            secret = (root / name).read_text().strip()
            require(secret not in metadata and secret not in history)
        require(str(root) not in metadata and str(root) not in history)
    for forbidden in (
        str(root),
        "release@example.invalid",
        "postgresql://",
        "postgresql+psycopg://",
    ):
        require(forbidden not in logs)
    for name in ("status.json", "accepted.json", "first.json", "candidate.json"):
        text = (root / name).read_text()
        require(
            str(root) not in text
            and "password" not in text
            and "example.invalid" not in text
        )


def exercise(root: Path, first: dict, second: dict) -> None:
    print("REHEARSAL_PREFLIGHT", flush=True)
    release = Release(root, first)
    release.preflight()
    try:
        # Absent secret and wrong trust/TLS fail before any application start.
        for name in ("runtime_password", "server.crt"):
            path = root / name
            data = path.read_bytes()
            path.unlink()
            try:
                invoke(root, root / "first.json", success=False)
            finally:
                path.write_bytes(data)
                path.chmod(0o444)
        for change in (
            {"backend": "sha256:" + "0" * 64},
            {"schema": "incompatible"},
            {"backend": first["frontend"]},
            {"commit": "0" * 40},
        ):
            write_json(root / "bad.json", first | change)
            invoke(root, root / "bad.json", success=False)
        certificate = root / "server.crt"
        original_certificate = certificate.read_bytes()
        certificate.unlink()
        certificate.write_bytes(b"INVALID SYNTHETIC TLS CERTIFICATE")
        certificate.chmod(0o444)
        try:
            invoke(root, root / "first.json", success=False)
        finally:
            certificate.unlink()
            certificate.write_bytes(original_certificate)
            certificate.chmod(0o444)
        # Requiring absent SMTP/scanner/signature refresh cannot be called PASS.
        for field in ("smtp", "clamav", "signature_update"):
            write_json(root / "modes.json", MODES | {field: "required"})
            invoke(root, root / "first.json", success=False)
            write_json(root / "modes.json", MODES)
            (root / "modes.json").chmod(0o444)
        invoke(root, root / "first.json")
        print("REHEARSAL_FIXTURE", flush=True)
        fixture(release)
        before = snapshot(release)
        release.compose("restart", "api", "worker", "nginx")
        release.compose("up", "--no-build", "-d", "--wait", "api", "worker", "nginx")
        require(snapshot(release) == before)
        # Actual database authentication failure during the mandatory migration.
        path = root / "migrator_password"
        original = path.read_bytes()
        path.unlink()
        path.write_text(secrets.token_urlsafe(48))
        path.chmod(0o444)
        try:
            output = invoke(root, root / "candidate.json", success=False)
            require(
                "RELEASE_BLOCKED_MIGRATION" in output and "RELEASE_START" not in output
            )
            require(
                not release.compose(
                    "ps", "--status", "running", "-q", "api", "worker", "nginx"
                )
            )
        finally:
            path.unlink()
            path.write_bytes(original)
            path.chmod(0o444)
        # Reject a version that has not previously passed acceptance.
        invoke(root, root / "candidate.json", rollback=True, success=False)
        invoke(root, root / "candidate.json")
        require(snapshot(release) == before)
        invoke(root, root / "first.json", rollback=True)
        require(snapshot(release) == before)
        # DB loss and unknown schema must fail the REAL runtime readiness probe.
        release.compose("stop", "postgres")
        try:
            import subprocess

            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    "/dev/null",
                    "-f",
                    str(REPO / "compose.release.yml"),
                    "-p",
                    release.config["project"],
                    "run",
                    "--rm",
                    "--no-deps",
                    "probe",
                ],
                env=release.env,
                capture_output=True,
                timeout=30,
            )
            require(result.returncode != 0)
        finally:
            release.compose("up", "--no-build", "-d", "--wait", "postgres")
        # Hide migration metadata temporarily, rather than forging/stamping a
        # version or touching domain rows. This is isolated fault injection.
        release.sql(
            "ALTER TABLE app.alembic_version RENAME TO rehearsal_version_hidden"
        )
        try:
            output = invoke(root, root / "first.json", rollback=True, success=False)
            require("RELEASE_BLOCKED_ROLLBACK_COMPATIBILITY" in output)
        finally:
            release.sql(
                "ALTER TABLE app.rehearsal_version_hidden RENAME TO alembic_version"
            )
        invoke(root, root / "first.json", rollback=True)
        require(snapshot(release) == before)
        verify_leaks(release, root, first, second)
        print("REHEARSAL_PASSED_SYNTHETIC_ONLY_REAL_STAGING_BLOCKED", flush=True)
    finally:
        # No down -v, delete, reset, stamp, downgrade, restore or backup.
        release.compose("stop")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Prepare a fresh private root and exact-HEAD immutable candidates; do not start services or seed",
    )
    parser.add_argument(
        "--profile", choices=(REDUCED_PROFILE, FULL_PROFILE), default=REDUCED_PROFILE
    )
    args = parser.parse_args()
    try:
        for variable in (
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        ):
            os.environ.pop(variable, None)
        os.environ["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        root = args.root or Path(tempfile.mkdtemp(prefix="akbriz-release-"))
        if args.root:
            root.mkdir(mode=0o700, exist_ok=False)
        require(
            os.environ.get("RELEASE_EXPECTED_HEAD", run(["git", "rev-parse", "HEAD"]))
            == run(["git", "rev-parse", "HEAD"])
        )
        prepare(root, profile=args.profile)
        first, second = build(root)
        if args.prepare_only:
            print("REHEARSAL_PREPARED_SYNTHETIC_ONLY_PRODUCTION_BLOCKED", flush=True)
            return 0
        if args.profile == FULL_PROFILE:
            from release_full_rehearsal import exercise_full

            exercise_full(root, first, second)
        else:
            exercise(root, first, second)
        return 0
    except Exception:
        print("REHEARSAL_BLOCKED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
