"""Operator-only attachment to a pre-existing, full synthetic local staging.

No application deploy, migrations, seeds, remote addresses or fault injection.
A private, short-lived authorization packet binds host, source, immutable images,
Compose ownership and the exact bounded profile. Missing evidence fails closed.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
import warnings
from datetime import UTC, datetime, timedelta
from getpass import GetPassWarning, getpass
from pathlib import Path
from urllib.parse import urlsplit

from mvp048_result import PROFILES, closed
from synthetic_stack import SyntheticStack, isolated_environment

FIELDS = {
    "schema",
    "head",
    "profile_digest",
    "host_identity_sha256",
    "operator_reference_sha256",
    "expires_at",
    "project",
    "images",
    "origin",
    "ca_file",
    "dataset_sha256",
    "synthetic_only",
    "ssd_attested",
    "backup_receipt",
    "runtime_journal",
}


def checked_packet(value, *, head, host_digest, now):
    closed(value, FIELDS)
    if value["schema"] != "MVP048_AUTHORIZATION/v1" or value["head"] != head:
        raise ValueError("TARGET_SOURCE_BLOCKED")
    if (
        not re.fullmatch("[0-9a-f]{40}", head)
        or value["host_identity_sha256"] != host_digest
    ):
        raise ValueError("TARGET_IDENTITY_BLOCKED")
    if (
        value["synthetic_only"] is not True
        or value["profile_digest"] != PROFILES["pilot200-v1"].digest
    ):
        raise ValueError("TARGET_PROFILE_BLOCKED")
    for key in ("host_identity_sha256", "operator_reference_sha256", "dataset_sha256"):
        if not re.fullmatch("[0-9a-f]{64}", value[key]):
            raise ValueError("TARGET_ATTESTATION_BLOCKED")
    expires = datetime.fromisoformat(value["expires_at"])
    if expires.tzinfo is None or not now < expires <= now + timedelta(hours=2):
        raise ValueError("TARGET_AUTHORIZATION_EXPIRED")
    if not re.fullmatch(r"akbrizcase-https-test-[0-9a-f]{10}", value["project"]):
        raise ValueError("TARGET_PROJECT_BLOCKED")
    closed(value["images"], {"backend", "frontend", "postgres", "generator"})
    if any(
        not re.fullmatch("sha256:[0-9a-f]{64}", v) for v in value["images"].values()
    ):
        raise ValueError("TARGET_IMAGES_BLOCKED")
    origin = urlsplit(value["origin"])
    if (
        origin.scheme != "https"
        or origin.hostname != "localhost"
        or origin.path
        or origin.query
        or origin.fragment
        or origin.username
        or origin.password
        or not origin.port
    ):
        raise ValueError("TARGET_TLS_BLOCKED")
    if value["origin"] != "https://localhost:" + str(origin.port):
        raise ValueError("TARGET_TLS_BLOCKED")
    if not Path(value["ca_file"]).is_absolute():
        raise ValueError("TARGET_TLS_BLOCKED")
    if value["ssd_attested"] is not True:
        raise ValueError("TARGET_DISK_ATTESTATION_BLOCKED")
    for key in ("backup_receipt", "runtime_journal"):
        if type(value[key]) is not str or not Path(value[key]).is_absolute():
            raise ValueError("TARGET_OPERATIONS_SOURCE_BLOCKED")
    return value


def read_packet(path, head):
    with os.fdopen(
        os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
    ) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.getuid()
            or info.st_size > 8192
        ):
            raise ValueError("TARGET_PRIVATE_AUTHORIZATION_REQUIRED")
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise ValueError("TARGET_AUTHORIZATION_INVALID")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("TARGET_DUPLICATE_FIELD")
            result[key] = value
        return result

    packet = checked_packet(
        json.loads(raw, object_pairs_hook=pairs),
        head=head,
        host_digest=hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest(),
        now=datetime.now(UTC),
    )
    ca = Path(packet["ca_file"])
    if ca.is_symlink() or not ca.is_file() or ca.stat().st_size > 65536:
        raise ValueError("TARGET_TLS_BLOCKED")
    return packet, hashlib.sha256(raw).hexdigest()


def attach(path, *, head):
    packet, digest = read_packet(path, head)
    environment = isolated_environment(os.environ)
    for key in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        environment.pop(key, None)
    # Force the local Unix daemon; saved Docker contexts cannot select a remote.
    environment["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    project = packet["project"]

    def docker(*args, input_text=None):
        return subprocess.run(
            ["docker", "--host=unix:///var/run/docker.sock", *args],
            input=input_text,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout.strip()

    services = {}
    for name in ("api", "worker", "nginx", "postgres"):
        ids = docker(
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=" + project,
            "--filter",
            "label=com.docker.compose.service=" + name,
        ).splitlines()
        if len(ids) != 1:
            raise ValueError("TARGET_SERVICES_BLOCKED")
        services[name] = json.loads(docker("inspect", ids[0]))[0]
    if not all(services[s]["State"]["Running"] for s in ("api", "nginx", "postgres")):
        raise ValueError("TARGET_SERVICES_BLOCKED")
    if services["worker"]["State"]["Running"]:
        raise ValueError("TARGET_OPERATOR_QUIESCE_WORKER_REQUIRED")
    for service, image_key in (
        ("api", "backend"),
        ("nginx", "frontend"),
        ("postgres", "postgres"),
    ):
        if services[service]["Image"] != packet["images"][image_key]:
            raise ValueError("TARGET_IMAGES_BLOCKED")
        if service != "postgres":
            image = json.loads(docker("image", "inspect", services[service]["Image"]))[
                0
            ]
            if (image["Config"].get("Labels") or {}).get(
                "org.opencontainers.image.revision"
            ) != head:
                raise ValueError("TARGET_IMAGE_REVISION_BLOCKED")
    from mvp048_smoke import GENERATOR_IMAGE

    if (
        json.loads(docker("image", "inspect", GENERATOR_IMAGE))[0]["Id"]
        != packet["images"]["generator"]
    ):
        raise ValueError("TARGET_GENERATOR_IMAGE_BLOCKED")
    for name in ("api", "worker", "postgres"):
        if services[name]["HostConfig"]["PortBindings"]:
            raise ValueError("TARGET_PRIVATE_NETWORK_BLOCKED")
    ports = services["nginx"]["NetworkSettings"]["Ports"]
    expected_binding = {
        "HostIp": "127.0.0.1",
        "HostPort": str(urlsplit(packet["origin"]).port),
    }
    if ports.get("8443/tcp") != [expected_binding] or any(
        binding["HostIp"] != "127.0.0.1"
        for values in ports.values()
        if values
        for binding in values
    ):
        raise ValueError("TARGET_LOOPBACK_TLS_REQUIRED")
    api_env = dict(item.split("=", 1) for item in services["api"]["Config"]["Env"])
    if (
        api_env.get("APP_ENV") != "demo"
        or api_env.get("DB_HOST", "postgres") != "postgres"
        or api_env.get("DB_NAME", "akbrizcase") != "akbrizcase"
    ):
        raise ValueError("TARGET_SYNTHETIC_CONFIGURATION_BLOCKED")
    # A deliberately narrow adapter: this repository's complete synthetic stack,
    # including exact stock Nginx policy. compose.release.yml cannot qualify.
    root = Path(__file__).resolve().parents[1]
    if (
        docker("exec", services["nginx"]["Id"], "cat", "/etc/nginx/nginx.conf").strip()
        != (root / "infrastructure/nginx.https.conf").read_text().strip()
    ):
        raise ValueError("TARGET_PROXY_POLICY_BLOCKED")

    def compose(*args, input_text=None):
        action = args[0]
        if action == "ps":
            names = [n for n in args[1:] if n in services]
            return "\n".join(services[n]["Id"] for n in names)
        if action == "exec" and args[:3] == ("exec", "-T", "postgres"):
            return docker(
                "exec",
                "-i",
                services["postgres"]["Id"],
                *args[3:],
                input_text=input_text,
            )
        if action == "exec" and args[:3] == ("exec", "-T", "api"):
            return docker(
                "exec", "-i", services["api"]["Id"], *args[3:], input_text=input_text
            )
        if action == "logs":
            return docker("logs", services["api"]["Id"])
        raise ValueError("TARGET_MUTATION_NOT_ALLOWED")

    stack = SyntheticStack(project, compose, environment)
    stack.claimed = True  # Read-only adoption; NEVER call cleanup on this stack.
    stack.verify_database()
    if stack.sql(
        "SELECT version_num FROM app.alembic_version"
    ) != "0011_mvp039_http_security" or not stack.sql(
        "SHOW server_version_num"
    ).startswith("16"):
        raise ValueError("TARGET_SCHEMA_BLOCKED")
    if (
        stack.sql(
            "SELECT "
            + "+".join(
                "(SELECT count(*) FROM app." + t + ")"
                for t in (
                    "orders",
                    "consents",
                    "chat_sessions",
                    "chat_messages",
                    "email_outbox",
                    "rate_limit_buckets",
                )
            )
        )
        != "0"
    ):
        raise ValueError("TARGET_EMPTY_SYNTHETIC_DATABASE_REQUIRED")
    # Bind every stored synthetic business/content field to the operator-reviewed
    # digest. Do not export rows. The owner marker alone is insufficient.
    tables = (
        "catalog_items",
        "equipment_models",
        "documents",
        "document_versions",
        "document_sections",
        "diag_trees",
        "consent_texts",
        "consent_publications",
    )
    rows = stack.sql(
        "SELECT jsonb_build_array("
        + ",".join(
            "(SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM app."
            + t
            + " t)"
            for t in tables
        )
        + ")"
    )
    if hashlib.sha256(rows.encode()).hexdigest() != packet["dataset_sha256"]:
        raise ValueError("TARGET_DATASET_BINDING_BLOCKED")
    if (
        stack.sql(
            "SELECT count(*) FROM app.catalog_items WHERE sku"
            " NOT LIKE 'DEMO-%' AND sku NOT LIKE "
            "'SYNTHETIC035-LIMIT-%'"
        )
        != "0"
    ):
        raise ValueError("TARGET_DATASET_BLOCKED")
    if (
        stack.sql(
            "SELECT count(*) FROM app.document_sections WHERE"
            " text <> 'Синтетическийквантопылесос: точная "
            "цитата для проверки.'"
        )
        != "0"
    ):
        raise ValueError("TARGET_DATASET_BLOCKED")
    model = stack.sql("SELECT id FROM app.equipment_models WHERE code='SYNTHETIC035'")
    document = stack.sql("SELECT id FROM app.documents WHERE code='SYNTHETIC035'")
    # Complete global TLS/readiness/mode preflight before any source can issue
    # its first mutating request. These are bounded, read-only HTTP probes.
    import ssl
    from urllib.request import HTTPSHandler, ProxyHandler, build_opener

    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context(cafile=packet["ca_file"])),
    )

    def read(path):
        with opener.open(packet["origin"] + path, timeout=8) as response:
            if response.status != 200:
                raise ValueError("TARGET_READINESS_BLOCKED")
            return json.loads(response.read(1024 * 1024))

    for path in ("/healthz", "/readyz"):
        if read(path) != {"status": "ok"}:
            raise ValueError("TARGET_READINESS_BLOCKED")
    policy = read("/api/v1/privacy-policy")
    if (
        policy.get("synthetic") is not True
        or policy.get("collection_enabled") is not True
    ):
        raise ValueError("TARGET_COLLECTION_BLOCKED")
    scenarios = read("/api/v1/chat/scenarios?model_id=" + model)
    if [v["key"] for v in scenarios["items"]] != ["SYNTHETIC035"]:
        raise ValueError("TARGET_FULL_PROFILE_BLOCKED")
    # No credentials in packet/argv/env/history/report. Only non-echoing operator
    # input, checked by core.operations inside the existing API before any load.
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        login = getpass("Acting staging admin login: ")
        password = getpass("Acting staging admin password: ")
    from mvp048_smoke import run_load

    config = {
        "origin": packet["origin"],
        "passwords": {"admin": password},
        "operator_login": login,
        "model_id": model,
        "document_id": document,
        "ca_file": packet["ca_file"],
        "authorization_digest": digest,
        "ssd_attested": packet["ssd_attested"],
        "backup_receipt": packet["backup_receipt"],
        "runtime_journal": packet["runtime_journal"],
    }
    # Verified TLS/readiness occurs inside every source before session creation.
    run_load(environment, config, compose, stack, None, None, target=True)
    return 0
