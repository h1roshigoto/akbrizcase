"""Protected operator aggregates. No public diagnostics endpoint or raw row export."""

import json
import math
import os
import shutil
import stat
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from getpass import GetPassWarning
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.auth import has_role
from app.core.cli import SafeParser, hidden
from app.core.db import Database, DatabaseSettings
from app.core.observability import checked_event, configure_logging
from app.core.staff import authenticate_operator
from app.core.staff_models import AuditEvent

MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_EVENTS = 50000


def _read(path: Path, maximum: int) -> bytes:
    with os.fdopen(
        os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb"
    ) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError
    return value


def runtime_report(path: Path | None, now: datetime) -> dict[str, Any]:
    if path is None:
        return {"status": "unavailable", "signal": "RUNTIME_JOURNAL_NOT_CONFIGURED"}
    try:
        lines = _read(path, MAX_JOURNAL_BYTES).splitlines()
        if not lines or len(lines) > MAX_JOURNAL_EVENTS:
            raise ValueError
        events = [checked_event(json.loads(line)) for line in lines]
        # The report covers exactly this operator-selected, bounded 24h sample;
        # it is not a persisted lifetime counter or a completeness assertion.
        if any(datetime.fromisoformat(e["at"]) > now for e in events):
            raise ValueError
        recent = [
            e
            for e in events
            if datetime.fromisoformat(e["at"]) >= now - timedelta(hours=24)
        ]
        http = [e for e in recent if e["event_code"] == "HTTP_REQUEST"]
        api = [
            e
            for e in http
            if e["endpoint"] not in {"health", "readiness", "admin", "other"}
        ]
        latencies = sorted(e["duration_ms"] for e in api)
        codes = Counter(e["event_code"] for e in recent)
        failures = {
            code: count
            for code, count in sorted(codes.items())
            if code
            in {
                "API_ERROR",
                "FILE_ERROR",
                "DB_ERROR",
                "SMTP_FAILURE",
                "SMTP_CONFIG_INVALID",
                "WORKER_TICK_FAILED",
                "CLEANUP_SCAN_FAILED",
                "CLEANUP_APPLY_FAILED",
                "CLEANUP_JOURNAL_FAILED",
                "CLEANUP_JOURNAL_REQUIRED",
                "CLEANUP_RECONCILIATION",
                "RATE_LIMIT_CLEANUP_FAILED",
            }
        }
        return {
            "status": "observed" if recent else "unavailable",
            "window_hours": 24,
            "coverage": "operator_selected_sample",
            "sample_events": len(recent),
            "http_requests": len(http),
            "http_5xx": sum(e["status"] >= 500 for e in http),
            "http_status_counts": dict(
                sorted(Counter(str(e["status"]) for e in http).items())
            ),
            "api_latency_ms": {
                "count": len(api),
                "mean": round(sum(latencies) / len(api), 3),
                "p95": latencies[math.ceil(0.95 * len(api)) - 1],
            }
            if api
            else {"status": "unavailable"},
            "failure_events": failures,
            "worker": "failure_observed"
            if codes["WORKER_TICK_FAILED"]
            else (
                "observed"
                if any(k.startswith("WORKER_") for k in codes)
                else "unavailable"
            ),
            "smtp": "failure_observed"
            if codes["SMTP_FAILURE"] or codes["SMTP_CONFIG_INVALID"]
            else (
                "disabled"
                if codes["SMTP_DISABLED"]
                else "sent_observed"
                if codes["SMTP_SENT"]
                else "unavailable"
            ),
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return {"status": "unavailable", "signal": "RUNTIME_JOURNAL_INVALID"}


def backup_report(path: Path | None, now: datetime) -> dict[str, Any]:
    if path is None:
        return {"status": "not_configured", "signal": "BACKUP_NOT_CONFIGURED"}
    try:
        receipt = json.loads(_read(path, 4096))
        if set(receipt) != {"status", "checked_at"} or receipt["status"] not in {
            "success",
            "failure",
        }:
            raise ValueError
        at = datetime.fromisoformat(receipt["checked_at"])
        if at.tzinfo is None or at > now:
            raise ValueError
        age = int((now - at).total_seconds())
        return {
            "status": receipt["status"],
            "age_seconds": age,
            "signal": "BACKUP_FAILURE"
            if receipt["status"] == "failure"
            else "BACKUP_CONFIRMATION_STALE"
            if age > 86400
            else "BACKUP_CONFIRMATION_OBSERVED",
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return {
            "status": "unavailable",
            "signal": "BACKUP_CONFIRMATION_MISSING_OR_INVALID",
        }


def disk_report(path: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        low = usage.free < 100 * 1024 * 1024 or usage.free / usage.total < 0.05
        return {
            "status": "low" if low else "observed",
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "signal": "DISK_LOW" if low else "DISK_SPACE_OBSERVED",
        }
    except (OSError, ValueError, ZeroDivisionError):
        return {"status": "unavailable", "signal": "DISK_UNAVAILABLE"}


def authorize(session: Session, login: str, password: str) -> str:
    actor = authenticate_operator(session, login, password)
    code = (
        "authentication_denied"
        if actor is None
        else ("success" if has_role(actor.user, "admin") else "forbidden")
    )
    session.add(
        AuditEvent(
            actor_id=actor.user.id if actor else None,
            action="operations.observe",
            entity_type="runtime",
            result=code,
            details={},
        )
    )
    session.flush()
    return code


def database_report(session: Session) -> dict[str, Any]:
    # Fixed category counts only; no text/UUID/number/response is selected.
    orders = {
        kind: {status: 0 for status in ("new", "in_progress", "closed", "cancelled")}
        for kind in ("commercial", "support")
    }
    for kind, status, count in session.execute(
        text("SELECT kind,status,count(*) FROM app.orders GROUP BY kind,status")
    ):
        if kind in orders and status in orders[kind]:
            orders[kind][status] = count
    outcomes = {key: 0 for key in ("resolved", "handoff", "unresolved", "unset")}
    for outcome, count in session.execute(
        text(
            "SELECT coalesce(outcome,'unset'),count(*) "
            "FROM app.chat_sessions GROUP BY outcome"
        )
    ):
        if outcome in outcomes:
            outcomes[outcome] = count
    queue = {key: 0 for key in ("pending", "sending", "sent", "failed")}
    for status, count in session.execute(
        text("SELECT status,count(*) FROM app.email_outbox GROUP BY status")
    ):
        if status in queue:
            queue[status] = count
    age = session.scalar(
        text(
            "SELECT extract(epoch FROM clock_timestamp()-min(created_at)) "
            "FROM app.email_outbox WHERE status='pending'"
        )
    )
    oldest = max(0, int(age)) if age is not None else None
    errors = int(
        session.scalar(
            text(
                "SELECT count(*) FROM app.email_outbox "
                "WHERE last_error_code IS NOT NULL"
            )
        )
        or 0
    )
    connections = session.scalar(
        text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()")
    )
    return {
        "status": "observed",
        "coverage": "currently_retained_rows",
        "orders": orders,
        "order_count": sum(sum(v.values()) for v in orders.values()),
        "chat_sessions": sum(outcomes.values()),
        "chat_technical_outcomes": outcomes,
        "outcome_meaning": "routing_state_only",
        "answer_usefulness": {
            "status": "unavailable",
            "reason": "explicit_feedback_source_absent",
        },
        "sales_conversion": {"status": "unavailable"},
        "postgres_connections": connections,
        "outbox": {
            "counts": queue,
            "oldest_pending_seconds": oldest,
            "rows_with_last_error": errors,
            "signals": (
                ["OUTBOX_PENDING_OVER_24H"]
                if oldest is not None and oldest > 86400
                else []
            )
            + (["OUTBOX_FAILED"] if queue["failed"] else [])
            + (["OUTBOX_DELIVERY_ERROR"] if errors else []),
        },
    }


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = SafeParser(description=__doc__)
    parser.add_argument("--runtime-journal", type=Path)
    parser.add_argument("--backup-receipt", type=Path)
    args = parser.parse_args(argv)
    database = None
    try:
        login = hidden("Acting admin login: ")
        password = hidden("Acting admin password: ")
        database = Database(DatabaseSettings.from_environment())
        if not database.ready():
            print('{"status":"unavailable","signal":"DB_UNAVAILABLE"}')
            return 1
        with database.unit_of_work() as session:
            code = authorize(session, login, password)
            report = database_report(session) if code == "success" else None
        if report is None:
            print('{"status":"denied","signal":"OPERATOR_AUTHORIZATION_DENIED"}')
            return 1
        now = datetime.now(UTC)
        output = {
            "database": report,
            "runtime": runtime_report(args.runtime_journal, now),
            "disk": disk_report(
                Path(os.environ.get("APP_FILES_DIR", "/var/lib/akbrizcase/files"))
            ),
            "backup": backup_report(args.backup_receipt, now),
        }
        print(json.dumps(output, sort_keys=True, allow_nan=False))
        return 0
    except (GetPassWarning, EOFError, KeyboardInterrupt):
        print('{"status":"unavailable","signal":"SECURE_INPUT_UNAVAILABLE"}')
        return 1
    except (SQLAlchemyError, OSError, ValueError):
        print('{"status":"unavailable","signal":"OPERATOR_REPORT_FAILED"}')
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    sys.exit(main())
