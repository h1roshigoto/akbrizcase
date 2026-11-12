"""Full-profile acceptance on the existing release controller and linked browser.

Private inputs and captured child output never become CI artifacts. All data,
mail and expert packets are synthetic; no target load or real infrastructure.
"""

import json
import os
import secrets
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

from https_smoke import browser_trust
from mvp046_browser_smoke import run_browser
from release import FULL_MODES, Release, read_json, require, run, write_json
from release_rehearsal import invoke, verify_leaks


def execute(release, code, payload=None, *, service="api", args=()):
    return release.compose(
        "exec",
        "-T",
        service,
        "python",
        "-c",
        code,
        *args,
        input_text=json.dumps(payload) if payload is not None else None,
    )


def seed(release):
    # The controller alone marked the newly created volume. Refuse reuse before
    # invoking the existing catalog, consent, staff, file and publication services.
    require(release.sql("SELECT count(*) FROM app.orders") == "0")
    require(release.sql("SELECT count(*) FROM app.staff_users") == "0")
    passwords = {role: secrets.token_urlsafe(32) for role in ("admin", "manager")}
    print("FULL_REHEARSAL_SEED_CATALOG_STAFF", flush=True)
    execute(
        release,
        """
import json, sys
from app.core.db import Database, DatabaseSettings
from app.core.staff import bootstrap_admin, create_staff
from app.core.staff import authenticate_operator
from app.modules.catalog.seed import seed_catalog
from app.modules.catalog.models import CatalogItem
from app.modules.catalog.write_dto import ItemFields, ItemEdit
from app.modules.catalog.write_service import CatalogWriteService
from app.modules.orders.seed import seed_consent
from sqlalchemy import select
d=Database(DatabaseSettings.from_environment())
seed_catalog(d, mode='full-synthetic-staging')
seed_consent(d, mode='full-synthetic-staging')
p=json.load(sys.stdin)
with d.unit_of_work() as s:
 assert bootstrap_admin(s,login='browser-admin',password=p['admin']).ok
 assert create_staff(s,admin_login='browser-admin',admin_password=p['admin'],login='browser-manager',password=p['manager'],role='manager').ok
 actor=authenticate_operator(s,'browser-admin',p['admin'])
 assert actor
 # The linked MVP-046 journey deliberately mixes fixed and unknown prices.
 # Match that existing fixture through its owning service, including revision.
 row=s.scalars(select(CatalogItem).where(CatalogItem.sku=='DEMO-S-001')).one()
 fields={key:getattr(row,key) for key in ItemFields.model_fields}
 fields.update(price_mode='on_request',price=None)
 CatalogWriteService(s,actor).edit_item(row.id,ItemEdit(**fields,expected_revision=row.revision))
d.close()
""",
        passwords,
    )
    # Same synthetic-only privilege preparation as the linked HTTPS fixture;
    # migrations/ordinary API startup do not grant consent publication rights.
    release.sql(
        "GRANT INSERT, UPDATE, DELETE ON app.consent_texts TO akbrizcase_runtime; GRANT INSERT ON app.consent_publications TO akbrizcase_runtime"
    )
    print("FULL_REHEARSAL_SEED_CONTENT", flush=True)
    content = json.loads(
        execute(
            release,
            Path("scripts/chat_browser_fixture.py").read_text(),
            {"login": "browser-admin", "password": passwords["admin"]},
            args=("--existing-operator",),
        )
    )
    print("FULL_REHEARSAL_SEED_READY", flush=True)
    return {
        "origin": f"https://localhost:{release.config['port']}",
        "passwords": passwords,
        **content,
    }


def retained(release):
    tables = (
        "orders",
        "order_items",
        "order_status_history",
        "consents",
        "documents",
        "document_versions",
        "document_sections",
        "catalog_items",
        "staff_users",
    )
    graph = release.sql(
        "SELECT md5(concat_ws('|',"
        + ",".join(
            f"(SELECT string_agg(row_to_json(t)::text, ',' ORDER BY id) FROM app.{table} t)"
            for table in tables
        )
        + "))"
    )
    outbox = release.sql(
        "SELECT md5(string_agg(concat_ws('|',id,order_id,event_kind,created_at), ',' ORDER BY id)) FROM app.email_outbox"
    )
    files = execute(
        release,
        """
import hashlib,json,os
from pathlib import Path
root=Path(os.environ['APP_FILES_DIR'])
print(hashlib.sha256(json.dumps(sorted((str(p.relative_to(root)),hashlib.sha256(p.read_bytes()).hexdigest()) for p in root.rglob('*') if p.is_file()),sort_keys=True).encode()).hexdigest())
""",
    )
    return graph, outbox, files


def files_and_resources(release):
    # Actual safe/infected ClamAV plus sandbox work overlapping Argon2 in the
    # same bounded API container. The test composition never enters app images.
    execute(
        release,
        """
import sys,io,concurrent.futures,threading
sys.path.insert(0,'/opt/release')
from browser_media_server import release_scanner
from app.modules.kb.files import FilePipeline,FileRejected
from argon2 import PasswordHasher
barrier=threading.Barrier(6)
def scan(infected):
 barrier.wait()
 p=FilePipeline.from_environment()
 try:
  p.accept(io.BytesIO(b'SYNTHINFECTED' if infected else b'SYNTHETIC SAFE RELEASE FILE'),filename='safe.txt',mime='text/plain')
 except FileRejected:
  assert infected
 else:
  assert not infected
def password():
 barrier.wait()
 hasher=PasswordHasher()
 value=hasher.hash('SYNTHETIC-RESOURCE-CHECK-ONLY')
 assert hasher.verify(value,'SYNTHETIC-RESOURCE-CHECK-ONLY')
with release_scanner(), concurrent.futures.ThreadPoolExecutor(6) as pool:
 futures=[pool.submit(scan,n==0) for n in range(3)] + [pool.submit(password) for _ in range(3)]
 for future in futures: future.result(timeout=60)
print('PASS')
""",
    )


def exercise_full(root: Path, first: dict, second: dict) -> None:
    release = Release(root, first)
    release.preflight()
    resource_samples = []
    sampling_done = threading.Event()

    def sample():
        while not sampling_done.is_set():
            try:
                ids = release.compose("ps", "-q", "api", "worker", "postgres").split()
                if ids:
                    data = run(
                        [
                            "docker",
                            "stats",
                            "--no-stream",
                            "--format",
                            "{{json .}}",
                            *ids,
                        ],
                        env=release.env,
                        timeout=10,
                    )
                    resource_samples.extend(
                        json.loads(line) for line in data.splitlines()
                    )
            except Exception:
                pass  # Sampling cannot authorize a release or hide test failures.
            sampling_done.wait(1)

    sampler = threading.Thread(target=sample, daemon=True)
    try:
        print("FULL_REHEARSAL_PREFLIGHT", flush=True)
        for name in ("runtime_password", "server.crt", "modes.json"):
            path = root / name
            original = path.read_bytes()
            path.unlink()
            try:
                invoke(root, root / "first.json", success=False)
            finally:
                path.write_bytes(original)
                path.chmod(0o444)
        for change in (
            {"real_pii": True},
            {"orders": 1},
            {"profile": "production"},
            {"extra": False},
            {"smtp": "external"},
        ):
            write_json(root / "modes.json", FULL_MODES | change)
            invoke(root, root / "first.json", success=False)
        write_json(root / "modes.json", FULL_MODES)
        (root / "modes.json").chmod(0o444)
        invoke(root, root / "first.json")
        original_config = read_json(root / "staging.json")
        write_json(
            root / "staging.json",
            original_config | {"project": "akbriz-staging-ffffffffffff"},
        )
        try:
            invoke(root, root / "first.json", success=False)
        finally:
            write_json(root / "staging.json", original_config)
        sampler.start()
        release.compose("stop", "worker")
        print("FULL_REHEARSAL_FILES_RESOURCES", flush=True)
        files_and_resources(release)
        print("FULL_REHEARSAL_LINKED_BROWSER", flush=True)
        config = seed(release)

        def verify_database():
            require(
                release.sql(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=current_database()"
                )
                == "akbrizcase:synthetic-seed:demo"
            )

        stack = SimpleNamespace(verify_database=verify_database, sql=release.sql)

        def smtp_probe(operation):
            require(operation in {"fail", "send"})
            output = release.compose(
                "run",
                "--rm",
                "--no-deps",
                "worker",
                "python",
                "/opt/release/notification_retry_probe.py",
                operation,
            )
            result = json.loads(output)
            require(result["result"] == "PASS")
            return result

        environment = os.environ.copy()
        environment["NODE_EXTRA_CA_CERTS"] = str(root / "ca.crt")
        with browser_trust(root / "ca.crt"):
            print("FULL_REHEARSAL_BROWSER_START", flush=True)
            result = run_browser(
                environment,
                config,
                release.compose,
                stack,
                [],
                str(root),
                smtp_probe=smtp_probe,
            )
            require(result.returncode == 0)
            browser_secrets = json.loads(result.stdout)["secrets"]
            # Separate focused staff acceptance, after the linked suite's fixed
            # business-count assertions. Passwords enter through stdin only.
            child = subprocess.run(
                ["node", "frontend/tests/release-staff.mjs"],
                env=environment,
                input=json.dumps(config),
                text=True,
                capture_output=True,
                timeout=120,
            )
            if child.returncode:
                print("FULL_REHEARSAL_STAFF_FAILED", flush=True)
            require(child.returncode == 0)
        before = retained(release)
        print("FULL_REHEARSAL_DEPENDENCIES", flush=True)
        release.compose("stop", "smtp-test")
        require(release.http("/readyz")[0] == 503)
        release.compose("up", "--no-build", "-d", "--wait", "smtp-test")
        signature = root / "signatures" / "test.ndb"
        original_signature = signature.read_bytes()
        (root / "signatures").chmod(0o700)
        signature.unlink()
        try:
            require(release.http("/readyz")[0] == 503)
            invoke(root, root / "first.json", success=False)
        finally:
            signature.write_bytes(original_signature)
            signature.chmod(0o444)
            (root / "signatures").chmod(0o555)
        # Corrupting the owner marker closes API readiness and direct CLI/UoW.
        release.sql("COMMENT ON DATABASE akbrizcase IS 'SYNTHETIC-FOREIGN-MARKER'")
        try:
            require(release.http("/readyz")[0] == 503)
            try:
                execute(
                    release,
                    "from app.core.db import Database,DatabaseSettings; d=Database(DatabaseSettings.from_environment());\nwith d.unit_of_work(): pass",
                )
            except Exception:
                pass
            else:
                raise AssertionError("Direct DB path admitted a foreign marker")
        finally:
            release.sql(
                "COMMENT ON DATABASE akbrizcase IS 'akbrizcase:synthetic-seed:demo'"
            )
        # Receipt/profile substitutions cannot grant rollback acceptance.
        accepted = read_json(root / "accepted.json")
        forged = json.loads(json.dumps(accepted))
        forged["releases"][0]["modes"] = "0" * 64
        write_json(root / "accepted.json", forged)
        try:
            invoke(root, root / "first.json", rollback=True, success=False)
        finally:
            write_json(root / "accepted.json", accepted)
        invoke(root, root / "candidate.json", rollback=True, success=False)
        secret_path = root / "migrator_password"
        original = secret_path.read_bytes()
        secret_path.unlink()
        secret_path.write_text(secrets.token_urlsafe(48))
        secret_path.chmod(0o444)
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
            secret_path.unlink()
            secret_path.write_bytes(original)
            secret_path.chmod(0o444)
        invoke(root, root / "candidate.json")
        require(retained(release) == before)
        invoke(root, root / "first.json", rollback=True)
        require(retained(release) == before)
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
        require(retained(release) == before)
        verify_leaks(
            release,
            root,
            first,
            second,
            extra=[
                *browser_secrets,
                *config["passwords"].values(),
                "example.invalid",
                "SYNTHETIC046 support",
                "SYNTHETIC046 Buyer",
            ],
        )
        require(resource_samples)
        # Only aggregate resource readings, never container names/IDs or payloads.
        print(
            "FULL_REHEARSAL_RESOURCES "
            + json.dumps(
                {
                    "samples": len(resource_samples),
                    "max_pids": max(int(r["PIDs"]) for r in resource_samples),
                    "memory_readings": sorted(
                        {r["MemUsage"] for r in resource_samples}
                    ),
                    "api_connection_ceiling": 24,
                    "max_cpu_percent": max(
                        float(r["CPUPerc"].rstrip("%")) for r in resource_samples
                    ),
                }
            ),
            flush=True,
        )
        print("FULL_REHEARSAL_PASSED_SYNTHETIC_ONLY_PRODUCTION_BLOCKED", flush=True)
    finally:
        sampling_done.set()
        if sampler.is_alive():
            sampler.join(timeout=15)
        release.compose("stop")
