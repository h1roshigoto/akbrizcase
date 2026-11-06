"""Closed runtime event schema. No message, exception or request serialization."""

import json
import logging
import math
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

EVENT_CODES = frozenset(
    {
        "RUNTIME_LOG_SUPPRESSED",
        "API_STARTED",
        "API_STOPPED",
        "HTTP_REQUEST",
        "API_ERROR",
        "DB_ERROR",
        "FILE_ERROR",
        "WORKER_STARTED",
        "WORKER_STOPPED",
        "WORKER_TICK_FAILED",
        "SMTP_CONFIG_INVALID",
        "SMTP_DISABLED",
        "SMTP_SENT",
        "SMTP_FAILURE",
        "CLEANUP",
        "CLEANUP_SCAN_FAILED",
        "CLEANUP_JOURNAL_REQUIRED",
        "CLEANUP_APPLIED",
        "CLEANUP_RECONCILIATION",
        "CLEANUP_APPLY_FAILED",
        "CLEANUP_JOURNAL_FAILED",
        "RATE_LIMIT_CLEANUP_FAILED",
    }
)
ENDPOINTS = frozenset(
    {
        "health",
        "readiness",
        "admin",
        "admin_api",
        "catalog",
        "chat",
        "orders",
        "knowledge",
        "api_other",
        "other",
    }
)
COUNTERS = frozenset({"processed", "skipped", "errors", "manual_review"})


def endpoint_category(path: str) -> str:
    if path == "/healthz":
        return "health"
    if path == "/readyz":
        return "readiness"
    for root, category in (
        ("/admin", "admin"),
        ("/api/v1/admin", "admin_api"),
        ("/api/v1/catalog", "catalog"),
        ("/api/v1/equipment-models", "catalog"),
        ("/api/v1/chat", "chat"),
        ("/api/v1/orders", "orders"),
        ("/api/v1/documents", "knowledge"),
        ("/api/v1/knowledge", "knowledge"),
        ("/api/v1/consent-text", "orders"),
        ("/api/v1/privacy-policy", "orders"),
    ):
        if path == root or path.startswith(root + "/"):
            return category
    return "api_other" if path.startswith("/api/v1/") else "other"


def checked_event(value: object) -> dict[str, Any]:
    """Also used when consuming operator-selected runtime journals."""
    if not isinstance(value, dict) or set(value) - {
        "event_code",
        "at",
        "request_id",
        "status",
        "endpoint",
        "duration_ms",
        *COUNTERS,
    }:
        raise ValueError("EVENT_SCHEMA_INVALID")
    if value.get("event_code") not in EVENT_CODES:
        raise ValueError("EVENT_SCHEMA_INVALID")
    at = datetime.fromisoformat(value["at"])
    if at.tzinfo is None:
        raise ValueError("EVENT_SCHEMA_INVALID")
    if "request_id" in value and str(UUID(value["request_id"])) != value["request_id"]:
        raise ValueError("EVENT_SCHEMA_INVALID")
    if "endpoint" in value and value["endpoint"] not in ENDPOINTS:
        raise ValueError("EVENT_SCHEMA_INVALID")
    if "status" in value and (
        type(value["status"]) is not int or not 100 <= value["status"] <= 599
    ):
        raise ValueError("EVENT_SCHEMA_INVALID")
    if "duration_ms" in value and (
        type(value["duration_ms"]) not in {int, float}
        or not math.isfinite(value["duration_ms"])
        or not 0 <= value["duration_ms"] <= 86400000
    ):
        raise ValueError("EVENT_SCHEMA_INVALID")
    for key in COUNTERS & value.keys():
        if type(value[key]) is not int or not 0 <= value[key] <= 2**63 - 1:
            raise ValueError("EVENT_SCHEMA_INVALID")
    if (
        value["event_code"] == "HTTP_REQUEST"
        and not {"request_id", "status", "endpoint", "duration_ms"} <= value.keys()
    ):
        raise ValueError("EVENT_SCHEMA_INVALID")
    return value


def emit(event_code: str, **fields: object) -> None:
    payload = checked_event(
        {"event_code": event_code, "at": datetime.now(UTC).isoformat(), **fields}
    )
    logging.getLogger("akbrizcase.runtime").info(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
    )


class SafeRuntimeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Never call getMessage/formatException on arbitrary library records.
        if record.name == "akbrizcase.runtime" and not record.args:
            try:
                return json.dumps(checked_event(json.loads(record.msg)), sort_keys=True)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                pass
        return json.dumps(
            {
                "event_code": "RUNTIME_LOG_SUPPRESSED",
                "at": datetime.now(UTC).isoformat(),
            }
        )


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(SafeRuntimeFormatter())
    logging.getLogger("akbrizcase.runtime").setLevel(logging.INFO)
    # These libraries can include raw URLs, SQL, DSNs or exception text. The
    # application emits safe fixed events at the corresponding boundaries.
    for name in (
        "uvicorn.error",
        "uvicorn.access",
        "sqlalchemy",
        "sqlalchemy.engine",
        "sqlalchemy.pool",
        "psycopg",
        "httpx",
        "httpcore",
    ):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = False
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)
