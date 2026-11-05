"""Synthetic canaries through real HTTP errors, logger formatting and operator I/O."""

import io
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.api_errors import install_api_errors
from app.core.audit_schema import validate_audit
from app.core.observability import SafeRuntimeFormatter, checked_event, emit
from app.core.operations import backup_report, disk_report, main, runtime_report

CANARIES = (
    "canary-contact@example.invalid",
    "+7 (999) 321-45-67",
    "canary-chat-token-7ca31",
    "canary-cookie-97323",
    "canary-q-922d1",
    "canary-reason-732f1",
    "<script>canary-malicious-911a2</script>",
)
RAW = " ".join(CANARIES)


def test_http_logs_and_errors_exclude_canaries() -> None:
    app = FastAPI()
    install_api_errors(app)

    @app.get("/api/v1/chat/probe/{value}")
    def probe(value: str, q: str, kind: str) -> None:
        if kind == "db":
            raise OperationalError(RAW, {"sql": RAW}, RuntimeError(RAW))
        if kind == "http":
            raise HTTPException(400, RAW)
        if kind == "file":
            raise OSError(RAW)
        raise RuntimeError(RAW)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeRuntimeFormatter())
    logger = logging.getLogger("akbrizcase.runtime")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with TestClient(app) as client:
            for kind, status in [
                ("db", 503),
                ("http", 400),
                ("file", 500),
                ("unknown", 500),
            ]:
                response = client.get(
                    "/api/v1/chat/probe/canary-path-459a3",
                    params={"q": RAW, "kind": kind},
                    headers={
                        "X-Request-ID": RAW,
                        "Authorization": RAW,
                        "X-Chat-Token": RAW,
                        "X-Request-Token": RAW,
                        "Cookie": "session=" + CANARIES[3],
                    },
                )
                assert response.status_code == status
                assert response.headers["Cache-Control"] == "no-store"
                assert (
                    str(UUID(response.headers["X-Request-ID"]))
                    == response.json()["request_id"]
                )
                assert all(
                    v not in response.text + str(response.headers) for v in CANARIES
                )
            response = client.get("/api/v1/chat/probe/value", params={"q": RAW})
            assert response.status_code == 422
        output = stream.getvalue()
        assert all(v not in output for v in (*CANARIES, "canary-path-459a3"))
        events = [checked_event(json.loads(line)) for line in output.splitlines()]
        http = [e for e in events if e["event_code"] == "HTTP_REQUEST"]
        assert [e["status"] for e in http] == [503, 400, 500, 500, 422]
        assert all(e["endpoint"] == "chat" and e["duration_ms"] >= 0 for e in http)
        assert {"DB_ERROR", "FILE_ERROR", "API_ERROR"} <= {
            e["event_code"] for e in events
        }
    finally:
        logger.removeHandler(handler)


def test_formatter_never_formats_arbitrary_exception_or_extra() -> None:
    record = logging.LogRecord("dependency", logging.ERROR, RAW, 1, RAW, (RAW,), None)
    record.secret = RAW
    try:
        raise RuntimeError(RAW)
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()
    assert (
        SafeRuntimeFormatter().format(record)
        == '{"event_code":"RUNTIME_LOG_SUPPRESSED"}'
    )
    with pytest.raises(ValueError, match="EVENT_SCHEMA_INVALID"):
        emit("HTTP_REQUEST", q=RAW)


@pytest.mark.parametrize(
    "action,entity,result,details",
    [
        ("staff.login", "staff_user", "success", {"q": RAW}),
        ("staff.login", "staff_user", RAW, {}),
        ("staff.login", "staff_user", "success", {"role": RAW}),
        ("order.status.change", "order", "success", {"from_status": RAW}),
        ("applicability.invalidate", "catalog_item", "success", {"reason": RAW}),
        ("tree.preview", "diag_tree", "PREVIEW", {"issue_codes": [RAW]}),
        ("tree.preview", "diag_tree", "PREVIEW", {"source_ids": [RAW]}),
        ("kb.review", "document_version", "success", {"response": {"body": RAW}}),
        ("staff.access", "staff_user", "success", {"expert_name": RAW}),
        ("unknown." + RAW, "staff_user", "success", {}),
    ],
)
def test_audit_rejects_unbounded_metadata(
    action: str, entity: str, result: str, details: object
) -> None:
    with pytest.raises(ValueError, match="^AUDIT_SCHEMA_INVALID$"):
        validate_audit(action, entity, result, details)


def test_audit_preserves_existing_evidence_and_technical_flags() -> None:
    validate_audit(
        "kb.review",
        "document_version",
        "success",
        {
            "expert_name": "Synthetic Expert",
            "expert_reference": "SYNTHETIC:review:1",
            "expert_approved_on": "2026-01-01",
            "decision": "approved",
            "content_checked": True,
            "full_file_public_allowed": False,
            "checksum_sha256": "a" * 64,
            "document_id": str(uuid4()),
        },
    )
    validate_audit(
        "applicability.publish",
        "catalog_item",
        "success",
        {
            "after_public_allowed": True,
            "before_public_allowed": False,
            "review_event_id": str(uuid4()),
            "revision": 3,
        },
    )
    validate_audit("privacy.audit_maintenance", "retention_maintenance", "success", {})


def test_runtime_counts_status_latency_and_failures_without_feedback(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "runtime.jsonl"
    events = [
        {
            "event_code": "HTTP_REQUEST",
            "at": now.isoformat(),
            "request_id": str(uuid4()),
            "endpoint": "chat",
            "duration_ms": ms,
            "status": status,
        }
        for ms, status in [(10, 200), (20, 422), (30, 503)]
    ]
    for code in (
        "SMTP_FAILURE",
        "WORKER_TICK_FAILED",
        "CLEANUP_SCAN_FAILED",
        "DB_ERROR",
    ):
        events.append({"event_code": code, "at": now.isoformat()})
    path.write_text("\n".join(json.dumps(e) for e in events))
    report = runtime_report(path, now)
    assert report["http_requests"] == 3 and report["http_5xx"] == 1
    assert report["api_latency_ms"] == {"count": 3, "mean": 20, "p95": 30}
    assert report["smtp"] == report["worker"] == "failure_observed"
    assert report["failure_events"]["CLEANUP_SCAN_FAILED"] == 1
    assert "answer_usefulness" not in report
    path.write_text(
        json.dumps({"event_code": "API_ERROR", "at": now.isoformat(), "reason": RAW})
    )
    assert runtime_report(path, now)["signal"] == "RUNTIME_JOURNAL_INVALID"
    assert all(v not in json.dumps(runtime_report(path, now)) for v in CANARIES)
    assert runtime_report(None, now)["status"] == "unavailable"
    path.write_text(
        json.dumps(
            {"event_code": "API_ERROR", "at": (now - timedelta(days=2)).isoformat()}
        )
    )
    assert runtime_report(path, now)["status"] == "unavailable"


def test_backup_and_disk_failure_signals(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "receipt.json"
    assert backup_report(None, now)["status"] == "not_configured"
    assert (
        backup_report(path, now)["signal"] == "BACKUP_CONFIRMATION_MISSING_OR_INVALID"
    )
    for status, age, signal in [
        ("failure", 1, "BACKUP_FAILURE"),
        ("success", 90000, "BACKUP_CONFIRMATION_STALE"),
        ("success", 1, "BACKUP_CONFIRMATION_OBSERVED"),
    ]:
        path.write_text(
            json.dumps(
                {
                    "status": status,
                    "checked_at": (now - timedelta(seconds=age)).isoformat(),
                }
            )
        )
        assert backup_report(path, now)["signal"] == signal
    path.write_text(RAW)
    assert RAW not in json.dumps(backup_report(path, now))
    with patch("shutil.disk_usage", side_effect=OSError(RAW)):
        assert disk_report(tmp_path)["signal"] == "DISK_UNAVAILABLE"
    usage = type(shutil_usage := __import__("shutil").disk_usage(tmp_path))
    with patch("shutil.disk_usage", return_value=usage(1000, 999, 1)):
        assert disk_report(tmp_path)["signal"] == "DISK_LOW"
    assert shutil_usage.total > 0


def test_operator_cli_failure_never_echoes_arguments_or_database_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch("app.core.operations.hidden", return_value=RAW),
        patch(
            "app.core.operations.DatabaseSettings.from_environment",
            side_effect=ValueError(RAW),
        ),
    ):
        assert main([]) == 1
    assert all(v not in capsys.readouterr().out for v in CANARIES)
    with pytest.raises(SystemExit):
        main(["--" + RAW])
    output = capsys.readouterr()
    assert all(v not in output.out + output.err for v in CANARIES)
