"""Tail aggregation exports no identities and never drops failed attempts."""

import json
import runpy
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.core.timing import RequestTiming


def aggregate(**values: Any) -> dict[str, Any]:
    # Integration images intentionally contain app/tests, not host scripts.
    # Load this unit-only controller helper on execution, never on collection.
    function = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/perf001_timing_result.py")
    )["aggregate"]
    result: dict[str, Any] = function(**values)
    return result


def packet() -> dict[str, Any]:
    clients: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    for operation in ("button", "order", "catalog", "knowledge"):
        for index in range(20):
            identifier = str(uuid4())
            start = 1_000_000_000 + len(clients) * 1_000_000_000
            row = {
                "request_id": identifier,
                "operation": operation,
                "status": 200,
                "start_ns": start,
                "end_ns": start + (20 + index) * 1_000_000,
                "send_end_ns": start + 4_000_000,
                "headers_end_ns": start + 13_000_000,
                "body_end_ns": start + 15_000_000,
                "validation_end_ns": start + 17_000_000,
                "tcp_ms": 1.0,
                "tls_ms": 2.0,
            }
            clients.append(row)
            samples.append(
                {
                    "phase": "measurement",
                    **{
                        k: row[k] for k in ("operation", "status", "start_ns", "end_ns")
                    },
                }
            )
            metrics = dict(RequestTiming().metrics)
            metrics.update(
                asgi_start_ns=start + 6_000_000,
                handler_start_ns=start + 7_000_000,
                route_start_ns=start + 8_000_000,
                response_start_ns=start + 12_000_000,
                response_end_ns=start + 13_000_000,
                worker_calls=1,
                worker_ms=3.0,
                dispatch_ms=1.0,
                pool_acquire_ms=0.5,
                sql_ms=0.5,
                commit_ms=0.5,
            )
            events.append(
                {
                    "event_code": "HTTP_PHASES",
                    "at": datetime.now(UTC).isoformat(),
                    "request_id": identifier,
                    "status": 200,
                    "endpoint": "chat",
                    "duration_ms": 7.0,
                    "timings": metrics,
                }
            )
    return {
        "head": "a" * 40,
        "clients": clients,
        "events": events,
        "samples": samples,
        "sampler_intervals": [],
    }


def test_tail_and_privacy() -> None:
    data = packet()
    result = aggregate(**data)
    assert result["coverage"] == "COMPLETE"
    for operation, metrics in result["operations"].items():
        assert metrics["http_all_attempt_ms"]["p95"] == 38
        assert (
            metrics["p95_and_above"]["attempts"]
            == metrics["p95_and_above"]["count"]
            == 2
        )
        if operation in {"button", "order"}:
            assert metrics["all"]["framework_queue_ms"]["mean"] == 1
            assert metrics["all"]["worker_other_ms"]["mean"] == 1.5
            assert metrics["all"]["server_other_ms"]["mean"] == 2
    rendered = json.dumps(result)
    assert all(row["request_id"] not in rendered for row in data["clients"])
    assert "start_ns" not in rendered and "timings" not in rendered


def test_unmatched_error_visible_in_all_attempts_and_tail() -> None:
    data = packet()
    row = data["clients"][19]
    row["request_id"] = None
    row["status"] = 0
    data["samples"][19]["status"] = 0
    result = aggregate(**data)
    metrics = result["operations"]["button"]
    assert result["coverage"] == "INCOMPLETE"
    assert metrics["attempts"] == metrics["http_all_attempt_ms"]["count"] == 20
    assert metrics["unmatched"] == metrics["p95_and_above"]["unmatched"] == 1
    assert metrics["http_all_attempt_ms"]["p95"] == 38
    assert metrics["statuses"] == {"0": 1, "200": 19}


@pytest.mark.parametrize(
    "case", ["field", "numeric", "ledger", "duplicate", "client_duplicate", "clock"]
)
def test_tampering_rejected_without_echo(case: str) -> None:
    data = deepcopy(packet())
    if case == "field":
        data["clients"][0]["token"] = "private-secret-canary"
    elif case == "numeric":
        data["events"][0]["timings"]["sql_ms"] = "private-secret-canary"
    elif case == "ledger":
        data["clients"].pop()
    elif case == "duplicate":
        data["events"].append(data["events"][0])
    elif case == "client_duplicate":
        data["clients"][1]["request_id"] = data["clients"][0]["request_id"]
    else:
        data["clients"][0]["headers_end_ns"] = 1
    with pytest.raises(ValueError) as caught:
        aggregate(**data)
    assert "private-secret-canary" not in str(caught.value)


def test_pg_disabled_io_and_counter_reset_are_unavailable() -> None:
    summarize = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/perf001_timing_result.py")
    )["pg_summary"]
    before = {
        "stats_reset": None,
        "track_activities": 1,
        "track_io_timing": 0,
        "active_time_ms": 10,
        "sessions": 1,
        "xact_commit": 5,
        "xact_rollback": 0,
        "deadlocks": 0,
        "blks_read": 3,
        "blks_hit": 100,
        "blk_read_time_ms": 0,
        "blk_write_time_ms": 0,
    }
    after = before | {"active_time_ms": 14, "blks_hit": 120}
    result = summarize({"before": before, "after": after})
    assert result["status"] == "OBSERVED"
    assert result["deltas"]["active_time_ms"] == 4
    assert result["deltas"]["blk_read_time_ms"] is None
    result = summarize({"before": before, "after": after | {"stats_reset": 1234}})
    assert result["status"] == "UNAVAILABLE"
    assert all(value is None for value in result["deltas"].values())


def test_sampler_overlap_is_correlated_without_exporting_clocks() -> None:
    data = packet()
    row = data["clients"][19]
    data["sampler_intervals"] = [
        {
            "start_ns": row["start_ns"],
            "end_ns": row["end_ns"],
            "probe_ms": 20,
            "authorization_ms": 10,
            "argon2_ms": 5,
            "argon2_process_cpu_ms": 12,
        }
    ]
    result = aggregate(**data)
    assert result["sampler"]["observed_cycles"] == 1
    assert result["sampler"]["metrics"]["argon2_ms"]["mean"] == 5
    assert (
        result["operations"]["button"]["p95_and_above"]["metrics"][
            "sampler_overlap_ms"
        ]["mean"]
        == 19.5
    )
    assert "start_ns" not in json.dumps(result)


def test_failed_validation_is_not_dropped_or_changed_to_zero() -> None:
    data = packet()
    data["clients"][19]["status"] = data["samples"][19]["status"] = data["events"][19][
        "status"
    ] = 503
    data["clients"][19]["validation_end_ns"] = None
    result = aggregate(**data)
    metrics = result["operations"]["button"]
    assert result["coverage"] == "INCOMPLETE"
    assert metrics["matched"] == metrics["attempts"] == 20
    assert metrics["partial_client"] == 1
    assert metrics["statuses"] == {"200": 19, "503": 1}
    assert metrics["http_all_attempt_ms"]["p95"] == 38
    assert metrics["p95_and_above"]["count"] == 2
    assert metrics["all"]["client_validate_ms"]["count"] == 19
