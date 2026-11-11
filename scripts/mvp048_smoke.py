"""Bounded synthetic HTTP load, real network sources and real recovery deadlines.

Target mode is a fail-closed preflight entry point, never a remote deploy.
"""

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from https_smoke import main as https_stack
from mvp048_images import source_trees, verify_application_images
from mvp048_result import PROFILES, checked_outage, make_result, publish
from notification_retry_browser_smoke import prepare_smtp

ROOT = Path(__file__).resolve().parents[1]
GENERATOR_IMAGE = "python:3.12.14-alpine"
OPTIONS = None


def source_state():
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    dirty = bool(
        subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()
    )
    if os.environ.get("MVP048_EXPECTED_HEAD", head) != head:
        raise ValueError("MVP048_HEAD_MISMATCH")
    if os.environ.get("MVP048_EXPECTED_HEAD") and dirty:
        raise ValueError("MVP048_DIRTY_EXPECTED_HEAD")
    return head, dirty


def receive(processes, stage, deadline, ledger=None):
    selector = selectors.DefaultSelector()
    output = []
    try:
        for process in processes:
            selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise TimeoutError("MVP048_STAGE_TIMEOUT")
            for key, _ in selector.select(
                timeout=min(2, max(0, deadline - time.monotonic()))
            ):
                raw = key.fileobj.readline(4 * 1024 * 1024)
                if not raw:
                    raise ValueError("MVP048_CLIENT_INCOMPLETE")
                result = json.loads(raw)
                if result.get("stage") != stage:
                    if result.get("stage") == "failed":
                        print(
                            "MVP048_CLIENT_FAILURE_LINES "
                            + json.dumps(result.get("lines", [])),
                            flush=True,
                        )
                        from mvp048_result import check_sample

                        failed = [check_sample(s) for s in result.get("samples", [])]
                        if ledger is not None:
                            ledger.extend(failed)
                        if failed:
                            print(
                                "MVP048_LAST_ATTEMPT " + json.dumps(failed[-1]),
                                flush=True,
                            )
                    raise ValueError("MVP048_CLIENT_FAILED")
                selector.unregister(key.fileobj)
                output.append(result)
                if stage == "done" and ledger is not None:
                    from mvp048_result import check_sample

                    ledger.extend(check_sample(value) for value in result["samples"])
        if {r["source"] for r in output} != set(range(len(processes))):
            raise ValueError("MVP048_SOURCE_MISMATCH")
        return output
    finally:
        selector.close()


def signal(processes, command):
    for process in processes:
        process.stdin.write(json.dumps(command) + "\n")
        process.stdin.flush()


def cleanup_sources(stack, containers, processes):
    """Try every owned resource even after one cleanup failure; no volume actions."""
    success = True
    for process in processes:
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            success = False
    for identifier in containers:
        try:
            detail = json.loads(stack.docker("inspect", identifier))[0]
            if (
                detail["Config"]["Labels"].get("com.akbrizcase.load-owner")
                != stack.project
            ):
                success = False
                continue
            stack.docker("stop", "--time", "1", identifier)
            stack.docker("rm", identifier)
        except (subprocess.SubprocessError, ValueError, KeyError):
            success = False
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            success = False
        finally:
            process.stdout.close()
            process.stderr.close()
        success = success and process.returncode == 0
    return success


class Resources:
    def __init__(self, compose, stack, probe):
        self.compose, self.stack, self.probe = compose, stack, probe
        self.stopping = threading.Event()
        self.samples = []
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.previous_cpu = None
        self.started_monotonic = time.monotonic()
        self.sample_lock = threading.Lock()
        self.backup_status = "unavailable"
        self.runtime_status = "unavailable"
        self.pool_configuration = {
            "status": "unavailable",
            "source": "application_factory",
            "pool_size": None,
            "max_overflow": None,
            "timeout_seconds": None,
        }

    def snapshot(self):
        row = {}
        try:
            mem = {
                line.split(":")[0]: int(line.split()[1]) * 1024
                for line in Path("/proc/meminfo").read_text().splitlines()
            }
            row.update(
                host_ram_total_bytes=mem["MemTotal"],
                host_ram_available_bytes=mem["MemAvailable"],
                host_vcpu=os.cpu_count(),
            )
            usage = os.statvfs(ROOT)
            row.update(
                host_disk_total_bytes=usage.f_blocks * usage.f_frsize,
                host_disk_free_bytes=usage.f_bavail * usage.f_frsize,
            )
            cpu = [
                int(v)
                for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]
            ]
            current = (sum(cpu), cpu[3] + cpu[4])
            if self.previous_cpu and current[0] > self.previous_cpu[0]:
                row["host_cpu_busy_percent"] = 100 * (
                    1
                    - (current[1] - self.previous_cpu[1])
                    / (current[0] - self.previous_cpu[0])
                )
            self.previous_cpu = current
        except (OSError, ValueError, KeyError):
            pass
        try:
            ids = self.compose(
                "ps", "--quiet", "api", "nginx", "postgres", "worker"
            ).split()
            details = json.loads(self.stack.docker("inspect", *ids))
            row["container_oom"] = sum(d["State"]["OOMKilled"] for d in details)
            stats = self.stack.docker(
                "stats", "--no-stream", "--format", "{{json .}}", *ids
            )
            cpus, memories = [], []
            for line in stats.splitlines():
                data = json.loads(line)
                cpus.append(float(data["CPUPerc"].rstrip("%")))
                size = data["MemUsage"].split("/")[0].strip()
                match = re.fullmatch(r"([0-9.]+)([kMGT]?i?B)", size)
                scale = {
                    "B": 1,
                    "kB": 1000,
                    "MB": 1000**2,
                    "GB": 1000**3,
                    "TB": 1000**4,
                    "KiB": 1024,
                    "MiB": 1024**2,
                    "GiB": 1024**3,
                    "TiB": 1024**4,
                }[match[2]]
                memories.append(float(match[1]) * scale)
            row.update(
                container_cpu_percent=sum(cpus), container_ram_bytes=sum(memories)
            )
        except (subprocess.SubprocessError, ValueError, TypeError, KeyError):
            pass
        try:
            data = self.probe("report")
            self.pool_configuration = data.get("configuration", {}).get(
                "sqlalchemy_pool", self.pool_configuration
            )
            if data["disk"]["status"] in {"observed", "low"}:
                row["files_disk_total_bytes"] = data["disk"]["total_bytes"]
                row["files_disk_free_bytes"] = data["disk"]["free_bytes"]
            backup = data["backup"]
            self.backup_status = (
                "synthetic"
                if backup.get("scope") == "synthetic"
                else "observed"
                if backup.get("signal") == "BACKUP_CONFIRMATION_OBSERVED"
                else "failure"
                if backup.get("signal") == "BACKUP_FAILURE"
                else "stale"
                if backup.get("signal")
                in {"BACKUP_CONFIRMATION_STALE", "BACKUP_SNAPSHOT_STALE"}
                else "unavailable"
            )
            self.runtime_status = data["runtime"]["status"]
            runtime_data = data["runtime"]
            if self.runtime_status == "observed":
                row.update(
                    runtime_http_requests=runtime_data["http_requests"],
                    runtime_http_5xx=runtime_data["http_5xx"],
                    runtime_smtp_failures=runtime_data["failure_events"].get(
                        "SMTP_FAILURE", 0
                    ),
                    runtime_db_errors=runtime_data["failure_events"].get("DB_ERROR", 0),
                )
                if "p95" in runtime_data["api_latency_ms"]:
                    row["runtime_handler_p95_ms"] = runtime_data["api_latency_ms"][
                        "p95"
                    ]
            database = data["database"]
            row.update(
                pg_connections=database["postgres_connections"],
                pg_active=data["pg_active"],
                pg_max_connections=data["pg_max_connections"],
                outbox_attempts=data["outbox_attempts"],
            )
            row.update(
                {"outbox_" + k: v for k, v in database["outbox"]["counts"].items()}
            )
            age = database["outbox"]["oldest_pending_seconds"]
            if age is not None:
                row["oldest_pending_seconds"] = age
        except (subprocess.SubprocessError, ValueError, KeyError):
            pass
        self.record(row)

    def record(self, row):
        with self.sample_lock:
            self.samples.append(
                {
                    "at_seconds": time.monotonic() - self.started_monotonic,
                    **{key: value for key, value in row.items() if value is not None},
                }
            )

    def loop(self):
        while not self.stopping.is_set():
            self.snapshot()
            self.stopping.wait(3)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.thread.join(timeout=30)
        if self.thread.is_alive():
            raise TimeoutError("MVP048_RESOURCE_CLEANUP_TIMEOUT")

    def report(self, runtime=False):
        fields = {
            "host_vcpu",
            "host_ram_total_bytes",
            "host_ram_available_bytes",
            "host_cpu_busy_percent",
            "host_disk_total_bytes",
            "host_disk_free_bytes",
            "files_disk_total_bytes",
            "files_disk_free_bytes",
            "container_cpu_percent",
            "container_ram_bytes",
            "container_oom",
            "pg_connections",
            "pg_active",
            "pg_max_connections",
            "outbox_pending",
            "outbox_sending",
            "outbox_failed",
            "outbox_sent",
            "outbox_attempts",
            "oldest_pending_seconds",
            "runtime_http_requests",
            "runtime_http_5xx",
            "runtime_handler_p95_ms",
            "runtime_smtp_failures",
            "runtime_db_errors",
        }
        return {
            "configuration": {"sqlalchemy_pool": self.pool_configuration},
            "samples": len(self.samples),
            "metrics": {
                field: {
                    "status": "observed" if values else "unavailable",
                    "minimum": min(values) if values else None,
                    "maximum": max(values) if values else None,
                    "last": values[-1] if values else None,
                }
                for field in sorted(fields)
                for values in [[row[field] for row in self.samples if field in row]]
            },
            "runtime": "observed"
            if runtime or self.runtime_status == "observed"
            else "unavailable",
            "backup": self.backup_status,
            "disk_medium": "unavailable",
        }


def run_load(environment, config, compose, stack, prefix, temporary, *, target=False):
    head, dirty = source_state()
    profile = PROFILES[OPTIONS.profile]
    started = time.monotonic()
    deadline = started + profile.total_timeout
    stack.verify_database()
    services = {
        name: json.loads(
            stack.docker("inspect", compose("ps", "--all", "--quiet", name).strip())
        )[0]
        for name in ("api", "nginx", "postgres", "worker")
    }
    identities = verify_application_images(
        stack.docker, services, head, source_trees(head)
    )
    identities["postgres"] = services["postgres"]["Image"]
    identities["generator"] = json.loads(
        stack.docker("image", "inspect", GENERATOR_IMAGE)
    )[0]["Id"]
    identities["schema"] = stack.sql("SELECT version_num FROM app.alembic_version")
    if target and any(
        identities[key] != config["expected_images"][key]
        for key in ("backend", "frontend", "postgres", "generator")
    ):
        raise ValueError("TARGET_IMAGES_CHANGED")
    # Compose only; the legacy timestamp-changing probes are never invoked.
    if not target:
        prepare_smtp(
            config,
            compose,
            stack,
            prefix,
            temporary,
            backend_image=identities["backend"],
        )
        compose("stop", "smtp-test")
    assert stack.sql("SHOW server_version_num").startswith("16")
    assert stack.sql("SELECT count(*) FROM app.orders") == "0"
    corpus = dict(
        zip(
            ("items", "documents", "versions", "sections", "trees"),
            map(
                int,
                stack.sql(
                    "SELECT "
                    + ",".join(
                        "(SELECT count(*) FROM app." + table + ")"
                        for table in (
                            "catalog_items",
                            "documents",
                            "document_versions",
                            "document_sections",
                            "diag_trees",
                        )
                    )
                ).split("|"),
            ),
            strict=True,
        )
    )
    assert corpus == {
        "items": 54,
        "documents": 1,
        "versions": 1,
        "sections": 1,
        "trees": 1,
    }
    item_id, revision = stack.sql(
        "SELECT id,revision FROM app.catalog_items WHERE "
        "sku='SYNTHETIC035-LIMIT-00' AND "
        "status='published'"
    ).split("|")
    consent = stack.sql("SELECT version FROM app.consent_texts")
    network = stack.project + "_web"
    detail = json.loads(stack.docker("network", "inspect", network))[0]
    assert (
        detail["Internal"]
        and detail["Labels"]["com.docker.compose.project"] == stack.project
    )
    processes, containers, samples, configs = [], [], [], []
    probe_source = (ROOT / "scripts/mvp048_operations.py").read_text()

    def probe(operation, **extra):
        command = json.dumps(
            {
                "operation": operation,
                "login": config.get("operator_login", "browser-admin"),
                "password": config["passwords"]["admin"],
                **extra,
                **(
                    {key: config[key] for key in ("backup_receipt", "runtime_journal")}
                    if target and operation == "report"
                    else {}
                ),
            }
        )
        output = (
            compose(
                "exec", "-T", "api", "python", "-c", probe_source, input_text=command
            )
            if target or operation in {"report", "invariants"}
            else compose(
                "run",
                "--rm",
                "--no-deps",
                "worker",
                "python",
                "-c",
                probe_source,
                input_text=command,
            )
        )
        return json.loads(output)

    initial = probe("report")  # Authorization before the first HTTP load request.
    if target and (
        initial["disk"]["status"] != "observed"
        or initial["backup"].get("signal") != "BACKUP_CONFIRMATION_OBSERVED"
        or initial["runtime"]["status"] != "observed"
    ):
        raise ValueError("TARGET_OPERATIONAL_SOURCES_BLOCKED")
    resources = Resources(compose, stack, probe)
    outcomes = {
        key: "NOT RUN"
        for key in (
            "invariants",
            "https",
            "contracts",
            "rate_limits",
            "smtp",
            "lease",
            "database",
            "backup",
        )
    }
    outcomes["backup"] = "BLOCKED"
    complete = False
    runtime_observed = False
    source_addresses = set()
    try:
        stack.docker("image", "inspect", GENERATOR_IMAGE)
        for source in range(profile.sources):
            name = stack.project + "-load-" + str(source)
            identifier = stack.docker(
                "create",
                "--interactive",
                "--name",
                name,
                "--label",
                "com.akbrizcase.load-owner=" + stack.project,
                "--network",
                network,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "32",
                "--memory",
                "96m",
                "--cpus",
                "0.5",
                "--user",
                "10001:10001",
                "--mount",
                "type=bind,src="
                + str(ROOT / "scripts/mvp048_client.py")
                + ",dst=/load/client.py,readonly",
                "--mount",
                "type=bind,src="
                + str(config.get("ca_file", ROOT / ".certs/ca.crt"))
                + ",dst=/load/ca.crt,readonly",
                "--entrypoint",
                "python",
                identities["generator"],
                "/load/client.py",
            )
            containers.append(identifier)
        # Attach streams before starting the clients; no inherited API environment,
        # backend network, secrets, files volume or Docker socket in generators.
        for source, identifier in enumerate(containers):
            process = subprocess.Popen(
                ["docker", "start", "--attach", "--interactive", identifier],
                env=environment,
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
            )
            processes.append(process)
            configs.append(
                {
                    "source": source,
                    "client_ids": list(range(source, profile.clients, profile.sources)),
                    "measurement_seconds": profile.measurement_seconds,
                    "total_timeout": profile.total_timeout,
                    "model_id": config["model_id"],
                    "document_id": config["document_id"],
                    "item_id": item_id,
                    "revision": int(revision),
                }
            )
        for identifier in containers:
            for _ in range(100):
                client = json.loads(stack.docker("inspect", identifier))[0]
                if client["State"]["Running"]:
                    break
                time.sleep(0.05)
            else:
                raise ValueError("MVP048_SOURCE_NOT_RUNNING")
            assert set(client["NetworkSettings"]["Networks"]) == {network}
            if client["Image"] != identities["generator"]:
                raise ValueError("MVP048_GENERATOR_IMAGE_CHANGED")
            source_addresses.add(
                client["NetworkSettings"]["Networks"][network]["IPAddress"]
            )
            assert not client["HostConfig"]["PortBindings"]
        assert len(source_addresses) == profile.sources and "" not in source_addresses
        for process, config_value in zip(processes, configs, strict=True):
            signal([process], config_value)
        receive(processes, "ready", min(deadline, time.monotonic() + 120), samples)
        resources.start()
        print(
            "MVP048_STAGE measurement sources="
            + str(len(source_addresses))
            + " clients="
            + str(profile.clients),
            flush=True,
        )
        signal(processes, {"start_ns": time.monotonic_ns() + 3 * 10**9})
        receive(
            processes,
            "measured",
            min(deadline, time.monotonic() + profile.measurement_seconds + 45),
            samples,
        )
        signal(processes, {"functional": True})
        reports = receive(
            processes, "done", min(deadline, time.monotonic() + 90), samples
        )
        for report in reports:
            assert report["requests"] <= profile.max_requests_per_source
        expected = sum(report["orders"] for report in reports)
        assert expected == 3 * profile.sources + 2
        probe("invariants", expected=expected, consent=consent)
        outcomes.update(
            invariants="PASS", https="PASS", contracts="PASS", rate_limits="PASS"
        )
        if target:
            outcomes["backup"] = (
                "PASS" if resources.backup_status == "observed" else "BLOCKED"
            )
            complete = True
        else:
            # PERF-001 investigation runs AFTER measured HTTP and invariants.
            # Fresh synthetic stack only; every probe write rolls back. This
            # separate aggregate cannot affect MVP048_RESULT or target gates.
            diagnostic = json.loads(compose(
                "exec", "-T", "api", "python", "-c",
                (ROOT / "scripts/perf001_probe.py").read_text(),
            ))
            print("PERF001_PROBE " + json.dumps(diagnostic), flush=True)
            # HTTP side effects are verified before/after the controlled DB outage.
            # Only this freshly claimed DB is stopped; target mode cannot reach here.
            import ssl
            from urllib.error import HTTPError
            from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

            opener = build_opener(
                ProxyHandler({}),
                HTTPSHandler(
                    context=ssl.create_default_context(cafile=ROOT / ".certs/ca.crt")
                ),
            )

            def http_status(path, payload=None):
                request = Request(
                    config["origin"] + path,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Idempotency-Key": "00000000-0000-4000-8000-000000000048",
                        "X-Request-Token": "a" * 64,
                    },
                )
                try:
                    with opener.open(request, timeout=10) as response:
                        response.read()
                        return response.status
                except HTTPError as error:
                    data = json.loads(error.read())
                    assert set(data) == (
                        {"status"} if path == "/readyz" else {"error", "request_id"}
                    )
                    return error.code

            stack.verify_database()
            compose("stop", "postgres")
            try:
                assert http_status("/healthz") == 200
                assert http_status("/readyz") == 503
                assert http_status("/api/v1/catalog/items") == 503
                order = {
                    "kind": "commercial",
                    "source": "site",
                    "items": [
                        {
                            "item_id": item_id,
                            "qty": 1,
                            "expected_revision": int(revision),
                        }
                    ],
                    "contact": {
                        "name": "SYNTHETIC048 ONLY",
                        "email": "load@example.invalid",
                    },
                    "consent": {"version": consent, "accepted": True},
                    "confirmed": True,
                }
                assert http_status("/api/v1/orders", json.dumps(order).encode()) == 503
            finally:
                compose(
                    "up",
                    "--no-build",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    "60",
                    "postgres",
                )
            for _ in range(20):
                if http_status("/readyz") == 200:
                    break
                time.sleep(1)
            else:
                raise ValueError("MVP048_READINESS_NOT_RECOVERED")
            assert http_status("/api/v1/catalog/items") == 200
            probe("invariants", expected=expected, consent=consent)
            outcomes["database"] = "PASS"
            print(
                "MVP048_STAGE database-recovered; "
                "SMTP recovery waits for real 120s lease + 300s retry",
                flush=True,
            )
            outage_events = checked_outage(probe("outage"))
            compose("up", "--no-build", "--detach", "smtp-test")
            probe("crash")
            # Start the actual resident worker after the real crash. Do not manually
            # edit failed/pending/sending, next_attempt_at, attempts or lease_until.
            compose("up", "--no-build", "--detach", "worker")
            recovered_lease = False
            while time.monotonic() < deadline:
                row = stack.sql(
                    "SELECT count(*) FILTER (WHERE status='sent'), "
                    "count(*) FILTER (WHERE "
                    "last_error_code='LEASE_EXPIRED'), count(*) "
                    "FILTER (WHERE status='failed') FROM "
                    "app.email_outbox"
                ).split("|")
                assert int(row[2]) == 0
                recovered_lease |= int(row[1]) > 0
                if int(row[0]) == expected:
                    break
                time.sleep(3)
            else:
                raise TimeoutError("MVP048_RECOVERY_TIMEOUT")
            assert recovered_lease
            outcomes.update(smtp="PASS", lease="PASS")
            probe("invariants", expected=expected, consent=consent)
            # Consume only the safe runtime schema, never dump raw logs/artifacts.
            from collections import Counter

            runtime = Counter()
            runtime_events = list(outage_events)
            for line in compose(
                "logs", "--no-log-prefix", "--no-color", "api", "worker"
            ).splitlines():
                try:
                    event = json.loads(line)
                    from uuid import UUID

                    from mvp048_result import number

                    if event.get("event_code") == "HTTP_REQUEST":
                        UUID(event["request_id"])
                        number(event["duration_ms"])
                        runtime["http"] += 1
                    runtime[event.get("event_code")] += 1
                    runtime_events.append(event)
                except (ValueError, KeyError, TypeError):
                    continue
            runtime_observed = runtime["http"] > 0 and runtime["SMTP_SENT"] > 0
            assert runtime_observed
            runtime_summary = probe("report", runtime_events=runtime_events)["runtime"]
            if runtime_summary["status"] == "observed":
                resources.record(
                    {
                        "runtime_http_requests": runtime_summary["http_requests"],
                        "runtime_http_5xx": runtime_summary["http_5xx"],
                        "runtime_handler_p95_ms": runtime_summary["api_latency_ms"][
                            "p95"
                        ],
                        "runtime_smtp_failures": runtime_summary["failure_events"].get(
                            "SMTP_FAILURE", 0
                        ),
                        "runtime_db_errors": runtime_summary["failure_events"].get(
                            "DB_ERROR", 0
                        ),
                    }
                )
            complete = True
    finally:
        failure_in_flight = sys.exc_info()[0] is not None
        cleanup_ok = True
        if resources.thread.ident is not None:
            try:
                resources.stop()
            except TimeoutError:
                cleanup_ok = False
        cleanup_ok = cleanup_sources(stack, containers, processes) and cleanup_ok
        if not cleanup_ok:
            complete = False
            outcomes["contracts"] = "FAIL"
        resource_report = resources.report(runtime_observed)
        if config.get("ssd_attested"):
            resource_report["disk_medium"] = "operator_attested_ssd"
        result = make_result(
            head=head,
            dirty=dirty,
            profile=profile,
            samples=samples,
            complete=complete,
            identities=identities,
            corpus=corpus,
            resources=resource_report,
            outcomes=outcomes,
            source_ip_count=len(source_addresses),
            authorization_digest=config.get("authorization_digest"),
            environment="authorized_target"
            if target
            else "ci_synthetic"
            if os.environ.get("CI")
            else "local_synthetic",
        )
        if source_state() != (head, dirty):
            raise ValueError("MVP048_SOURCE_CHANGED_DURING_RUN")
        publish(
            result,
            samples,
            OPTIONS.report_dir,
            head,
            resource_samples=resources.samples,
            resource_origin_ns=round(resources.started_monotonic * 1e9),
        )
        print("MVP048_STAGE complete=" + str(complete).lower(), flush=True)
        if not failure_in_flight and (
            result["gates"]["SYNTHETIC_SMOKE"] != "PASS"
            or (target and result["gates"]["TARGET_LOAD"] != "PASS")
        ):
            raise ValueError("MVP048_EVIDENCE_NOT_PASS")


def target_preflight(path):
    # No target machine, operator and reviewed full staging attachment have been
    # supplied. Refuse before opening any socket, loading credentials or Compose.
    if path is None:
        return {
            "TARGET_LOAD": "BLOCKED",
            "reason": "TARGET_AUTHORIZATION_MISSING",
            "PRODUCTION_ADMISSION": "BLOCKED",
        }
    return {
        "TARGET_LOAD": "BLOCKED",
        "reason": "TARGET_LIVE_PREFLIGHT_REQUIRED",
        "PRODUCTION_ADMISSION": "BLOCKED",
    }


def main(argv=None):
    global OPTIONS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("synthetic", "target"))
    parser.add_argument("--profile", choices=PROFILES, default="pilot200-v1")
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--authorization", type=Path)
    OPTIONS = parser.parse_args(argv)
    if OPTIONS.mode == "target":
        if OPTIONS.authorization is None:
            print("MVP048_TARGET " + json.dumps(target_preflight(None)), flush=True)
            return 2
        head, dirty = source_state()
        if dirty or OPTIONS.profile != "pilot200-v1":
            raise ValueError("TARGET_CLEAN_APPROVED_PROFILE_REQUIRED")
        if OPTIONS.report_dir is None:
            OPTIONS.report_dir = Path(
                tempfile.mkdtemp(prefix="mvp048-target-evidence-")
            )
        from mvp048_target import attach

        return attach(OPTIONS.authorization, head=head)
    if OPTIONS.authorization is not None:
        parser.error("Authorization packets apply only to target preflight")
    if OPTIONS.report_dir is None:
        OPTIONS.report_dir = Path(tempfile.mkdtemp(prefix="mvp048-evidence-"))
    source_state()
    https_stack(mvp048=True)
    return 0


if __name__ == "__main__":
    sys.modules["mvp048_smoke"] = sys.modules[__name__]
    import signal as process_signal

    def interrupted(_signum, _frame):
        raise TimeoutError("MVP048_CANCELLED_OR_WATCHDOG")

    for signum in (
        process_signal.SIGTERM,
        process_signal.SIGINT,
        process_signal.SIGALRM,
    ):
        process_signal.signal(signum, interrupted)
    process_signal.alarm(1200)
    try:
        sys.exit(main())
    except Exception as error:
        import traceback

        frames = traceback.extract_tb(error.__traceback__)
        print(
            "MVP048_FAILURE "
            + type(error).__name__
            + " "
            + ",".join(Path(f.filename).name + ":" + str(f.lineno) for f in frames),
            flush=True,
        )
        if isinstance(error, subprocess.CalledProcessError):
            diagnostic = (error.stdout or "") + (error.stderr or "")
            for needle, code in [
                ("No space left on device", "ENOSPC"),
                ("Permission denied", "PERMISSION_DENIED"),
                ("unhealthy", "SERVICE_UNHEALTHY"),
                ("cannot mount", "MOUNT_FAILED"),
            ]:
                if needle.lower() in diagnostic.lower():
                    print("MVP048_INFRA_" + code, flush=True)
        # Raw child output, SQL/SMTP exceptions and command arguments stay private.
        sys.exit(1)
