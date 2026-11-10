"""MVP048 closed evidence and deterministic measurement; standard library only."""

import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

OPERATIONS = {"button": 300, "knowledge": 800, "catalog": 800, "order": 1000}
PHASES = {"preflight", "prepare", "warmup", "measurement", "functional"}
STATUSES = {"PASS", "FAIL", "BLOCKED", "NOT RUN", "INCONCLUSIVE"}
MIN_SAMPLES = 20
SCHEMA = "MVP048_RESULT/v1"


@dataclass(frozen=True)
class Profile:
    name: str
    clients: int
    sources: int
    concurrency: int
    measurement_seconds: int
    request_timeout: int = 8
    total_timeout: int = 900
    orders_per_source: int = 5
    max_requests_per_source: int = 240
    max_corpus_items: int = 100

    def __post_init__(self):
        for field, maximum in (
            ("clients", 200),
            ("sources", 30),
            ("concurrency", 30),
            ("measurement_seconds", 120),
            ("request_timeout", 10),
            ("total_timeout", 1200),
            ("orders_per_source", 5),
            ("max_requests_per_source", 240),
            ("max_corpus_items", 100),
        ):
            number(getattr(self, field), 1, maximum, integer=True)
        if not self.concurrency == self.sources <= self.clients <= self.sources * 8:
            raise ValueError("PROFILE_INVALID")
        if self.name not in {"pilot200-v1", "single-ip-v1"}:
            raise ValueError("PROFILE_INVALID")

    @property
    def digest(self):
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()


def number(value, low=0, high=2**63 - 1, *, integer=False):
    if (
        type(value) not in ({int} if integer else {int, float})
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError("EVIDENCE_NUMBER_INVALID")
    return value


PROFILES = {
    "pilot200-v1": Profile("pilot200-v1", 200, 30, 30, 15),
    "single-ip-v1": Profile("single-ip-v1", 6, 1, 1, 10),
}


def closed(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError("EVIDENCE_FIELDS_INVALID")


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(number(v, 0, 120000) for v in values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def check_sample(sample):
    closed(
        sample,
        {
            "phase",
            "operation",
            "status",
            "ok",
            "timeout",
            "start_ns",
            "end_ns",
            "client",
        },
    )
    if sample["phase"] not in PHASES or sample["operation"] not in OPERATIONS:
        raise ValueError("EVIDENCE_SAMPLE_INVALID")
    number(sample["client"], 0, 199, integer=True)
    number(sample["start_ns"], 1, integer=True)
    number(sample["end_ns"], sample["start_ns"], integer=True)
    number((sample["end_ns"] - sample["start_ns"]) / 1e6, 0, 120000)
    number(sample["status"], 0, 599, integer=True)
    if type(sample["ok"]) is not bool or type(sample["timeout"]) is not bool:
        raise ValueError("EVIDENCE_SAMPLE_INVALID")
    if sample["ok"] and (sample["timeout"] or not 200 <= sample["status"] < 300):
        raise ValueError("EVIDENCE_SAMPLE_INVALID")
    if sample["timeout"] and sample["status"] != 0:
        raise ValueError("EVIDENCE_SAMPLE_INVALID")
    return sample


def overlap(samples):
    """Sweep real operation intervals; end precedes start at equal timestamps."""
    events = []
    for sample in samples:
        check_sample(sample)
        if sample["end_ns"] > sample["start_ns"]:
            events.extend([(sample["start_ns"], 1), (sample["end_ns"], -1)])
    current = peak = area = 0
    previous = min((at for at, _ in events), default=0)
    for at, delta in sorted(events):
        area += current * (at - previous)
        current += delta
        peak = max(peak, current)
        previous = at
    span = (max(at for at, _ in events) - min(at for at, _ in events)) if events else 0
    return {
        "peak_active": peak,
        "mean_active": area / span if span else 0,
        "active_operation_seconds": area / 1e9,
        "span_seconds": span / 1e9,
    }


def statistics(samples, operation, seconds):
    attempts = [
        check_sample(s)
        for s in samples
        if s["operation"] == operation and s["phase"] == "measurement"
    ]
    # Every attempt contributes to all-attempt percentiles; successes also have
    # a separately named distribution. Errors never disappear from the gate.
    all_ms = [(s["end_ns"] - s["start_ns"]) / 1e6 for s in attempts]
    successful = [v for s, v in zip(attempts, all_ms, strict=True) if s["ok"]]
    failures = sum(not s["ok"] for s in attempts)
    gate = (
        "FAIL"
        if failures
        else "INCONCLUSIVE"
        if len(successful) < MIN_SAMPLES
        else "PASS"
        if percentile(successful, 0.95) <= OPERATIONS[operation]
        else "FAIL"
    )

    def distribution(values):
        return {
            "p50": percentile(values, 0.5),
            "p95": percentile(values, 0.95),
            "p99": percentile(values, 0.99),
            "maximum": max(values, default=None),
        }

    return {
        "attempts": len(attempts),
        "successes": len(successful),
        "errors": failures,
        "timeouts": sum(s["timeout"] for s in attempts),
        "statuses": dict(sorted(Counter(str(s["status"]) for s in attempts).items())),
        "attempt_rps": len(attempts) / seconds if seconds else 0,
        "success_rps": len(successful) / seconds if seconds else 0,
        "all_attempt_ms": distribution(all_ms),
        "success_ms": distribution(successful),
        "minimum_samples": MIN_SAMPLES,
        "threshold_ms": OPERATIONS[operation],
        "gate": gate,
    }


def make_result(
    *,
    head,
    dirty,
    profile,
    samples,
    complete,
    identities,
    corpus,
    resources,
    outcomes,
    source_ip_count,
    environment="local_synthetic",
    authorization_digest=None,
):
    for sample in samples:
        check_sample(sample)
    measured = [s for s in samples if s["phase"] == "measurement"]
    activity = overlap(measured)
    seconds = max(profile.measurement_seconds, activity["span_seconds"])
    result = {
        "schema": SCHEMA,
        "head": head,
        "dirty": dirty,
        "environment": environment,
        "profile": asdict(profile),
        "profile_digest": profile.digest,
        "authorization_digest": authorization_digest,
        "complete": complete,
        "identities": identities,
        "corpus": corpus,
        "activity": activity,
        "measurement_seconds": seconds,
        "network": {
            "source_ip_count": source_ip_count,
            "nat_profile": "independent_container_sources"
            if profile.sources > 1
            else "single_source_nat",
            "client_pool_observed": len({s["client"] for s in measured}),
        },
        "phases": {
            phase: sum(s["phase"] == phase for s in samples) for phase in sorted(PHASES)
        },
        "phase_statuses": {
            phase: dict(
                sorted(
                    Counter(
                        str(s["status"]) for s in samples if s["phase"] == phase
                    ).items()
                )
            )
            for phase in sorted(PHASES)
        },
        "phase_seconds": {
            phase: overlap([s for s in samples if s["phase"] == phase])["span_seconds"]
            for phase in sorted(PHASES)
        },
        "operations": {op: statistics(samples, op, seconds) for op in OPERATIONS},
        "resources": resources,
        "outcomes": outcomes,
        "gates": {
            "SYNTHETIC_SMOKE": "NOT RUN",
            "TARGET_LOAD": "BLOCKED",
            "OPERATIONAL_RECOVERY": "BLOCKED",
            "PRODUCTION_ADMISSION": "BLOCKED",
        },
        "limitations": [
            "SYNTHETIC_CORPUS",
            *([] if authorization_digest else ["NO_TARGET_AUTHORIZATION"]),
            "NO_OFFSITE_EVIDENCE",
            "NO_PRODUCTION_ADMISSION",
        ],
    }
    result["gates"] = derive_gates(result)
    return validate_result(result, expected_head=head)


def derive_gates(r):
    good = (
        r["complete"]
        and not any(op["errors"] for op in r["operations"].values())
        and all(
            v == "PASS"
            for k, v in r["outcomes"].items()
            if k in {"invariants", "https", "contracts", "rate_limits"}
        )
    )
    smoke = "PASS" if good else "FAIL"
    if not r["complete"]:
        smoke = "INCONCLUSIVE"
    elif r["dirty"]:
        smoke = "INCONCLUSIVE"
    recovery = (
        "PASS"
        if all(r["outcomes"][k] == "PASS" for k in ("smtp", "lease", "database"))
        else "BLOCKED"
    )
    target = "BLOCKED"
    if r["environment"] == "authorized_target":
        enough = (
            r["profile"]["name"] == "pilot200-v1"
            and r["network"]["source_ip_count"] == 30
            and r["network"]["client_pool_observed"] == 200
            and r["activity"]["peak_active"] == 30
        )
        resources_ready = (
            r["resources"]["samples"] >= 2
            and all(
                v["status"] == "observed"
                for k, v in r["resources"]["metrics"].items()
                if k != "oldest_pending_seconds"
            )
            and r["resources"]["runtime"] == "observed"
            and r["resources"]["backup"] == "observed"
            and r["resources"]["disk_medium"] == "operator_attested_ssd"
        )
        if (
            good
            and not r["dirty"]
            and enough
            and resources_ready
            and r["authorization_digest"]
        ):
            target = (
                "PASS"
                if all(op["gate"] == "PASS" for op in r["operations"].values())
                else "FAIL"
                if any(op["gate"] == "FAIL" for op in r["operations"].values())
                else "INCONCLUSIVE"
            )
            for prefix in ("host", "files"):
                disk = r["resources"]["metrics"][prefix + "_disk_free_bytes"]["minimum"]
                total = r["resources"]["metrics"][prefix + "_disk_total_bytes"]["last"]
                if disk < 100 * 1024 * 1024 or not total or disk / total < 0.05:
                    target = "FAIL"
    return {
        "SYNTHETIC_SMOKE": smoke,
        "TARGET_LOAD": target,
        "OPERATIONAL_RECOVERY": recovery,
        "PRODUCTION_ADMISSION": "BLOCKED",
    }


def validate_result(r, *, expected_head):
    closed(
        r,
        {
            "schema",
            "head",
            "dirty",
            "environment",
            "authorization_digest",
            "profile",
            "profile_digest",
            "complete",
            "identities",
            "corpus",
            "activity",
            "network",
            "measurement_seconds",
            "phases",
            "phase_statuses",
            "phase_seconds",
            "operations",
            "resources",
            "outcomes",
            "gates",
            "limitations",
        },
    )
    if (
        r["schema"] != SCHEMA
        or not re.fullmatch(r"[0-9a-f]{40}", r["head"])
        or r["head"] != expected_head
    ):
        raise ValueError("EVIDENCE_HEAD_INVALID")
    if type(r["dirty"]) is not bool or type(r["complete"]) is not bool:
        raise ValueError("EVIDENCE_STATE_INVALID")
    if r["environment"] not in {"local_synthetic", "ci_synthetic", "authorized_target"}:
        raise ValueError("EVIDENCE_ENVIRONMENT_INVALID")
    if r["environment"] == "authorized_target":
        if type(r["authorization_digest"]) is not str or not re.fullmatch(
            "[0-9a-f]{64}", r["authorization_digest"]
        ):
            raise ValueError("EVIDENCE_TARGET_AUTHORIZATION_INVALID")
    elif r["authorization_digest"] is not None:
        raise ValueError("EVIDENCE_TARGET_AUTHORIZATION_INVALID")
    profile = PROFILES.get(r["profile"].get("name"))
    if (
        profile is None
        or r["profile"] != asdict(profile)
        or r["profile_digest"] != profile.digest
    ):
        raise ValueError("EVIDENCE_PROFILE_INVALID")
    closed(r["network"], {"source_ip_count", "nat_profile", "client_pool_observed"})
    number(r["network"]["source_ip_count"], 0, profile.sources, integer=True)
    number(r["network"]["client_pool_observed"], 0, profile.clients, integer=True)
    if r["network"]["nat_profile"] != (
        "independent_container_sources" if profile.sources > 1 else "single_source_nat"
    ):
        raise ValueError("EVIDENCE_NETWORK_INVALID")
    if r["complete"] and (
        r["network"]["source_ip_count"] != profile.sources
        or r["network"]["client_pool_observed"] != profile.clients
    ):
        raise ValueError("EVIDENCE_NETWORK_INCOMPLETE")
    closed(r["identities"], {"backend", "frontend", "postgres", "generator", "schema"})
    for key in ("backend", "frontend", "postgres", "generator"):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", r["identities"][key]):
            raise ValueError("EVIDENCE_IMAGE_INVALID")
    if r["identities"]["schema"] != "0011_mvp039_http_security":
        raise ValueError("EVIDENCE_SCHEMA_INVALID")
    closed(r["corpus"], {"items", "documents", "versions", "sections", "trees"})
    for value in r["corpus"].values():
        number(value, 1, 100, integer=True)
    closed(
        r["activity"],
        {"peak_active", "mean_active", "active_operation_seconds", "span_seconds"},
    )
    number(r["activity"]["peak_active"], 0, profile.concurrency, integer=True)
    number(r["activity"]["mean_active"], 0, r["activity"]["peak_active"])
    for key in ("active_operation_seconds", "span_seconds"):
        number(r["activity"][key], 0, profile.total_timeout * profile.concurrency)
    seconds = number(
        r["measurement_seconds"], profile.measurement_seconds, profile.total_timeout
    )
    closed(r["phases"], PHASES)
    for count in r["phases"].values():
        number(
            count, 0, profile.sources * profile.max_requests_per_source, integer=True
        )
    closed(r["phase_statuses"], PHASES)
    closed(r["phase_seconds"], PHASES)
    for phase in PHASES:
        number(r["phase_seconds"][phase], 0, profile.total_timeout)
        for code, count in r["phase_statuses"][phase].items():
            if not re.fullmatch(r"0|[1-5][0-9]{2}", code):
                raise ValueError("EVIDENCE_PHASE_STATUS_INVALID")
            number(
                count,
                1,
                profile.sources * profile.max_requests_per_source,
                integer=True,
            )
        if sum(r["phase_statuses"][phase].values()) != r["phases"][phase]:
            raise ValueError("EVIDENCE_PHASE_TOTALS_INVALID")
    closed(r["operations"], OPERATIONS)
    for name, op in r["operations"].items():
        closed(
            op,
            {
                "attempts",
                "successes",
                "errors",
                "timeouts",
                "statuses",
                "attempt_rps",
                "success_rps",
                "all_attempt_ms",
                "success_ms",
                "minimum_samples",
                "threshold_ms",
                "gate",
            },
        )
        for key in ("attempts", "successes", "errors", "timeouts"):
            number(op[key], 0, 7200, integer=True)
        if (
            op["successes"] + op["errors"] != op["attempts"]
            or op["timeouts"] > op["errors"]
        ):
            raise ValueError("EVIDENCE_TOTALS_INVALID")
        for code, count in op["statuses"].items():
            if not re.fullmatch(r"0|[1-5][0-9]{2}", code):
                raise ValueError("EVIDENCE_STATUS_INVALID")
            number(count, 1, 7200, integer=True)
        if (
            sum(op["statuses"].values()) != op["attempts"]
            or op["statuses"].get("0", 0) < op["timeouts"]
            or sum(n for code, n in op["statuses"].items() if not code.startswith("2"))
            > op["errors"]
        ):
            raise ValueError("EVIDENCE_TOTALS_INVALID")
        if (
            op["attempt_rps"] != op["attempts"] / seconds
            or op["success_rps"] != op["successes"] / seconds
        ):
            raise ValueError("EVIDENCE_RPS_INVALID")
        for key, count in (
            ("all_attempt_ms", op["attempts"]),
            ("success_ms", op["successes"]),
        ):
            closed(op[key], {"p50", "p95", "p99", "maximum"})
            values = [
                op[key][quantile] for quantile in ("p50", "p95", "p99", "maximum")
            ]
            if count == 0:
                if values != [None] * 4:
                    raise ValueError("EVIDENCE_SAMPLE_INVALID")
            else:
                for v in values:
                    number(v, 0, 120000)
                if values != sorted(values):
                    raise ValueError("EVIDENCE_PERCENTILE_INVALID")
        expected = (
            "FAIL"
            if op["errors"]
            else "INCONCLUSIVE"
            if op["successes"] < MIN_SAMPLES
            else "PASS"
            if op["success_ms"]["p95"] <= OPERATIONS[name]
            else "FAIL"
        )
        if (
            op["minimum_samples"] != MIN_SAMPLES
            or op["threshold_ms"] != OPERATIONS[name]
            or op["gate"] != expected
        ):
            raise ValueError("EVIDENCE_GATE_INVALID")
    if (
        sum(o["attempts"] for o in r["operations"].values())
        != r["phases"]["measurement"]
    ):
        raise ValueError("EVIDENCE_TOTALS_INVALID")
    closed(
        r["outcomes"],
        {
            "invariants",
            "https",
            "contracts",
            "rate_limits",
            "smtp",
            "lease",
            "database",
            "backup",
        },
    )
    if any(v not in STATUSES for v in r["outcomes"].values()):
        raise ValueError("EVIDENCE_OUTCOME_INVALID")
    validate_resources(r["resources"])
    closed(
        r["gates"],
        {
            "SYNTHETIC_SMOKE",
            "TARGET_LOAD",
            "OPERATIONAL_RECOVERY",
            "PRODUCTION_ADMISSION",
        },
    )
    if r["gates"] != derive_gates(r):
        raise ValueError("EVIDENCE_GATE_INVALID")
    if set(r["limitations"]) - {
        "SYNTHETIC_CORPUS",
        "NO_TARGET_AUTHORIZATION",
        "NO_OFFSITE_EVIDENCE",
        "NO_PRODUCTION_ADMISSION",
        "INCOMPLETE_RUN",
        "INSUFFICIENT_SAMPLES",
        "ONE_IP_NAT",
        "RESOURCES_UNAVAILABLE",
        "TARGET_ADAPTER_BLOCKED",
    }:
        raise ValueError("EVIDENCE_LIMITATION_INVALID")
    return r


def validate_resources(resources):
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
        "pg_pool_size",
        "pg_pool_overflow",
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
    closed(resources, {"samples", "metrics", "runtime", "backup", "disk_medium"})
    number(resources["samples"], 0, 2400, integer=True)
    closed(resources["metrics"], fields)
    for metric in resources["metrics"].values():
        closed(metric, {"status", "minimum", "maximum", "last"})
        if metric["status"] == "unavailable":
            if any(metric[k] is not None for k in ("minimum", "maximum", "last")):
                raise ValueError("EVIDENCE_RESOURCE_INVALID")
        elif metric["status"] == "observed":
            for k in ("minimum", "maximum", "last"):
                number(metric[k])
            if not metric["minimum"] <= metric["last"] <= metric["maximum"]:
                raise ValueError("EVIDENCE_RESOURCE_INVALID")
        else:
            raise ValueError("EVIDENCE_RESOURCE_INVALID")
    if (
        resources["runtime"] not in {"unavailable", "observed"}
        or resources["backup"]
        not in {"unavailable", "observed", "synthetic", "stale", "failure"}
        or resources["disk_medium"] not in {"unavailable", "operator_attested_ssd"}
    ):
        raise ValueError("EVIDENCE_RESOURCE_INVALID")


def private_write(path, value):
    # Exclusive create, no symlink following or overwrite of an earlier report.
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def publish(
    result,
    samples,
    directory,
    expected_head,
    *,
    resource_samples=None,
    resource_origin_ns=None,
):
    validate_result(result, expected_head=expected_head)
    points = resource_samples if resource_samples is not None else []
    if points:
        number(resource_origin_ns, 1, integer=True)
    if resource_samples is not None and len(points) != result["resources"]["samples"]:
        raise ValueError("EVIDENCE_RESOURCE_COUNT_INVALID")
    previous = 0
    for point in points:
        if (
            type(point) is not dict
            or "at_seconds" not in point
            or set(point) - {"at_seconds", *result["resources"]["metrics"]}
        ):
            raise ValueError("EVIDENCE_RESOURCE_POINT_INVALID")
        previous = number(
            point["at_seconds"], previous, result["profile"]["total_timeout"] + 60
        )
        for key, value in point.items():
            if key != "at_seconds":
                number(value)
    for sample in samples:
        check_sample(sample)
        if sample["client"] >= result["profile"]["clients"]:
            raise ValueError("EVIDENCE_CLIENT_POOL_INVALID")
    for phase in PHASES:
        group = [s for s in samples if s["phase"] == phase]
        if (
            result["phases"][phase] != len(group)
            or result["phase_statuses"][phase]
            != dict(sorted(Counter(str(s["status"]) for s in group).items()))
            or result["phase_seconds"][phase] != overlap(group)["span_seconds"]
        ):
            raise ValueError("EVIDENCE_PHASE_LEDGER_MISMATCH")
    if result["network"]["client_pool_observed"] != len(
        {s["client"] for s in samples if s["phase"] == "measurement"}
    ):
        raise ValueError("EVIDENCE_CLIENT_POOL_INVALID")
    # Rebuild statistics from the private attempt ledger before publication.
    for op in OPERATIONS:
        if result["operations"][op] != statistics(
            samples, op, result["measurement_seconds"]
        ):
            raise ValueError("EVIDENCE_LEDGER_MISMATCH")
    if result["activity"] != overlap(
        [s for s in samples if s["phase"] == "measurement"]
    ):
        raise ValueError("EVIDENCE_LEDGER_MISMATCH")
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError("EVIDENCE_DIRECTORY_NOT_PRIVATE")
    private_write(
        directory / "measurement.json",
        {
            "result": result,
            "attempts": samples,
            "resource_samples": points,
            "resource_origin_ns": resource_origin_ns,
        },
    )
    private_write(directory / "summary.json", result)
    print(
        "MVP048_RESULT " + json.dumps(result, sort_keys=True, allow_nan=False),
        flush=True,
    )
