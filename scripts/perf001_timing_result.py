"""Join numeric measurements inside the owned API container; export aggregates only.

No IDs, wall/monotonic timestamps, paths, headers, SQL or arbitrary strings occur
in the output. Percentiles of overlapping spans MUST NOT be added together.
"""

import json
import math
import re
import sys
from collections import Counter
from uuid import UUID

from app.core.observability import checked_event
from app.core.timing_schema import COUNTS, DURATIONS, checked_timings

OPS = {"button", "order", "catalog", "knowledge"}
CLIENT_FIELDS = {
    "request_id",
    "operation",
    "status",
    "start_ns",
    "end_ns",
    "send_end_ns",
    "headers_end_ns",
    "body_end_ns",
    "validation_end_ns",
    "tcp_ms",
    "tls_ms",
}


def number(value, limit=2**63 - 1):
    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or not 0 <= value <= limit
    ):
        raise ValueError("PERF001_NUMERIC_SCHEMA_INVALID")
    return value


def distribution(values):
    values = sorted(values)
    return {
        "count": len(values),
        "mean": sum(values) / len(values) if values else None,
        "p50": values[math.ceil(0.50 * len(values)) - 1] if values else None,
        "p95": values[math.ceil(0.95 * len(values)) - 1] if values else None,
        "maximum": max(values) if values else None,
    }


def checked_client(row):
    if (
        type(row) is not dict
        or set(row) != CLIENT_FIELDS
        or row["operation"] not in OPS
    ):
        raise ValueError("PERF001_CLIENT_SCHEMA_INVALID")
    if row["request_id"] is not None:
        if str(UUID(row["request_id"])) != row["request_id"]:
            raise ValueError("PERF001_CLIENT_SCHEMA_INVALID")
    if type(row["status"]) is not int or not 0 <= row["status"] <= 599:
        raise ValueError("PERF001_CLIENT_SCHEMA_INVALID")
    for key in CLIENT_FIELDS - {"request_id", "operation", "status"}:
        if row[key] is not None:
            number(row[key])
    return row


def derive(client, event):
    m = checked_timings(event["timings"])
    if event["status"] != client["status"]:
        raise ValueError("PERF001_STATUS_MISMATCH")
    stamps = [
        client[k]
        for k in (
            "start_ns",
            "send_end_ns",
            "headers_end_ns",
            "body_end_ns",
            "validation_end_ns",
            "end_ns",
        )
    ]
    if any(v is None for v in stamps) or stamps != sorted(stamps):
        raise ValueError("PERF001_CLIENT_CLOCK_INVALID")
    if not (
        0
        < m["asgi_start_ns"]
        <= m["handler_start_ns"]
        <= m["response_start_ns"]
        <= m["response_end_ns"]
    ):
        raise ValueError("PERF001_SERVER_CLOCK_INVALID")
    if (
        m["asgi_start_ns"] < client["start_ns"]
        or m["response_start_ns"] > client["end_ns"]
    ):
        raise ValueError("PERF001_CORRELATION_CLOCK_INVALID")
    result = {k: m[k] for k in DURATIONS | COUNTS}
    result.update(
        client_total_ms=(client["end_ns"] - client["start_ns"]) / 1e6,
        tcp_ms=client["tcp_ms"],
        tls_ms=client["tls_ms"],
        headers_wait_ms=(client["headers_end_ns"] - client["send_end_ns"]) / 1e6,
        body_read_ms=(client["body_end_ns"] - client["headers_end_ns"]) / 1e6,
        client_validate_ms=(client["validation_end_ns"] - client["body_end_ns"]) / 1e6,
        client_close_ms=(client["end_ns"] - client["validation_end_ns"]) / 1e6,
        server_ms=(m["response_end_ns"] - m["asgi_start_ns"]) / 1e6,
        framework_queue_ms=(m["handler_start_ns"] - m["asgi_start_ns"]) / 1e6,
        response_stream_ms=(m["response_end_ns"] - m["response_start_ns"]) / 1e6,
        # This signed residual includes transport/proxy/Uvicorn admission and
        # sender/receiver overlap. It is explicitly NOT a pure queue wait.
        send_to_asgi_signed_ms=(m["asgi_start_ns"] - client["send_end_ns"]) / 1e6,
    )
    if client["operation"] in {"button", "order"}:
        if client["status"] < 400 and (
            not m["route_start_ns"] or m["worker_calls"] != 1
        ):
            raise ValueError("PERF001_WORKER_COVERAGE_INVALID")
        result["route_delay_ms"] = (m["route_start_ns"] - m["handler_start_ns"]) / 1e6
        result["server_other_ms"] = (
            result["server_ms"]
            - m["worker_ms"]
            - m["dispatch_ms"]
            - result["framework_queue_ms"]
        )
        result["worker_other_ms"] = (
            m["worker_ms"]
            - (m["pool_acquire_ms"] - m["commit_pool_ms"])
            - (m["sql_ms"] - m["commit_sql_ms"])
            - m["commit_ms"]
        )
        result["worker_remainder_after_checkout_ms"] = (
            result["worker_other_ms"] - m["checkout_after_get_ms"]
        )
    else:
        for key in ("dispatch_ms", "worker_ms", "worker_cpu_ms"):
            result.pop(key)
    return result


def aggregate(head, clients, events, samples, sampler_intervals, postgres=None):
    if (
        re.fullmatch(r"[0-9a-f]{40}", head) is None
        or len(clients) > 7200
        or len(events) > 15000
    ):
        raise ValueError("PERF001_SCOPE_INVALID")
    for row in clients:
        checked_client(row)
    expected = Counter(
        (s["operation"], s["start_ns"], s["end_ns"], s["status"])
        for s in samples
        if s["phase"] == "measurement"
    )
    actual = Counter(
        (s["operation"], s["start_ns"], s["end_ns"], s["status"]) for s in clients
    )
    if expected != actual:
        raise ValueError("PERF001_ATTEMPT_LEDGER_MISMATCH")
    ids = [row["request_id"] for row in clients if row["request_id"] is not None]
    if len(ids) != len(set(ids)):
        raise ValueError("PERF001_DUPLICATE_CLIENT_ID")
    for interval in sampler_intervals:
        if (
            not {"start_ns", "end_ns"} <= interval.keys()
            or interval["end_ns"] < interval["start_ns"]
        ):
            raise ValueError("PERF001_SAMPLER_SCHEMA_INVALID")
        if set(interval) - {
            "start_ns",
            "end_ns",
            "probe_ms",
            "authorization_ms",
            "argon2_ms",
            "argon2_process_cpu_ms",
        }:
            raise ValueError("PERF001_SAMPLER_SCHEMA_INVALID")
        for value in interval.values():
            if value is not None:
                number(value)
    by_id = {}
    for event in events:
        checked_event(event)
        if event["event_code"] != "HTTP_PHASES" or event["request_id"] in by_id:
            raise ValueError("PERF001_EVENT_SCHEMA_INVALID")
        by_id[event["request_id"]] = event
    output = {
        "schema": "PERF001_PHASES/v1",
        "head": head,
        "scope": "MEASURED_HTTP",
        "operations": {},
    }
    for operation in sorted(OPS):
        attempts = [row for row in clients if row["operation"] == operation]
        joined = [
            derive(row, by_id[row["request_id"]])
            for row in attempts
            if row["request_id"] in by_id
        ]
        matched_rows = [r for r in attempts if r["request_id"] in by_id]
        for row, result in zip(matched_rows, joined, strict=True):
            result["sampler_overlap_ms"] = sum(
                max(
                    0,
                    min(row["end_ns"], s["end_ns"])
                    - max(row["start_ns"], s["start_ns"]),
                )
                / 1e6
                for s in sampler_intervals
            )
        total = distribution([(r["end_ns"] - r["start_ns"]) / 1e6 for r in attempts])
        expected_tail = (
            sum((r["end_ns"] - r["start_ns"]) / 1e6 >= total["p95"] for r in attempts)
            if attempts
            else 0
        )
        tail = (
            [v for v in joined if v["client_total_ms"] >= total["p95"]]
            if joined
            else []
        )
        keys = set().union(*(v.keys() for v in joined))
        output["operations"][operation] = {
            "attempts": len(attempts),
            "http_all_attempt_ms": total,
            "matched": len(joined),
            "unmatched": len(attempts) - len(joined),
            "statuses": dict(
                sorted(Counter(str(c["status"]) for c in attempts).items())
            ),
            "all": {
                k: distribution([v[k] for v in joined if k in v]) for k in sorted(keys)
            },
            "p95_and_above": {
                "attempts": expected_tail,
                "count": len(tail),
                "unmatched": expected_tail - len(tail),
                "metrics": {
                    k: distribution([v[k] for v in tail if k in v])
                    for k in sorted(keys)
                },
            },
        }
    if clients:
        low = min(r["start_ns"] for r in clients)
        high = max(r["end_ns"] for r in clients)
        active_samples = [
            s for s in sampler_intervals if s["start_ns"] <= high and s["end_ns"] >= low
        ]
    else:
        active_samples = []
    output["sampler"] = {
        "observed_cycles": len(active_samples),
        "metrics": {
            k: distribution([s[k] for s in active_samples if s.get(k) is not None])
            for k in (
                "probe_ms",
                "authorization_ms",
                "argon2_ms",
                "argon2_process_cpu_ms",
            )
        },
    }
    correlated = [
        by_id[c["request_id"]]["timings"] for c in clients if c["request_id"] in by_id
    ]
    if correlated and all(
        m["process_start_cpu_ns"] > 0 and m["process_end_cpu_ns"] > 0
        for m in correlated
    ):
        first = min(correlated, key=lambda m: m["asgi_start_ns"])
        last = max(correlated, key=lambda m: m["response_end_ns"])
        cpu_ms = (last["process_end_cpu_ns"] - first["process_start_cpu_ns"]) / 1e6
        wall_ms = (last["response_end_ns"] - first["asgi_start_ns"]) / 1e6
        if cpu_ms < 0 or wall_ms <= 0:
            raise ValueError("PERF001_PROCESS_CLOCK_INVALID")
        output["api_process_cpu"] = {
            "status": "OBSERVED",
            "cpu_ms": cpu_ms,
            "wall_ms": wall_ms,
            "core_equivalents": cpu_ms / wall_ms,
        }
    else:
        output["api_process_cpu"] = {
            "status": "UNAVAILABLE",
            "cpu_ms": None,
            "wall_ms": None,
            "core_equivalents": None,
        }
    output["postgres"] = pg_summary(postgres)
    output["coverage"] = (
        "COMPLETE"
        if all(
            v["attempts"] > 0 and not v["unmatched"]
            for v in output["operations"].values()
        )
        else "INCOMPLETE"
    )
    return output


def pg_summary(value):
    counters = {
        "active_time_ms",
        "sessions",
        "xact_commit",
        "xact_rollback",
        "deadlocks",
        "blks_read",
        "blks_hit",
        "blk_read_time_ms",
        "blk_write_time_ms",
    }
    unavailable = {
        "status": "UNAVAILABLE",
        "track_io_timing": None,
        "deltas": {k: None for k in sorted(counters)},
    }
    if value is None:
        return unavailable
    if set(value) != {"before", "after"}:
        raise ValueError("PERF001_PG_SCHEMA_INVALID")
    before, after = value["before"], value["after"]
    for row in (before, after):
        if type(row) is not dict or set(row) != counters | {
            "stats_reset",
            "track_activities",
            "track_io_timing",
        }:
            raise ValueError("PERF001_PG_SCHEMA_INVALID")
        for k, v in row.items():
            if k != "stats_reset" or v is not None:
                number(v)
    if (
        before["stats_reset"] != after["stats_reset"]
        or not before["track_activities"]
        or not after["track_activities"]
        or any(after[k] < before[k] for k in counters)
    ):
        return unavailable
    io = bool(before["track_io_timing"] and after["track_io_timing"])
    return {
        "status": "OBSERVED",
        "track_io_timing": io,
        "deltas": {
            k: after[k] - before[k]
            if io or k not in {"blk_read_time_ms", "blk_write_time_ms"}
            else None
            for k in sorted(counters)
        },
    }


if __name__ == "__main__":
    try:
        packet = json.load(sys.stdin)
        assert set(packet) == {
            "head",
            "clients",
            "events",
            "samples",
            "sampler_intervals",
            "postgres",
        }
        result = aggregate(**packet)
        print(json.dumps(result, sort_keys=True, allow_nan=False), flush=True)
        if result["coverage"] != "COMPLETE":
            raise SystemExit(1)
    except Exception:
        # Exception inputs can contain synthetic canaries or tampered raw fields.
        print("PERF001_PHASES_INVALID", file=sys.stderr)
        raise SystemExit(1) from None
