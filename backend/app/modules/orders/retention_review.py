"""Bounded, read-only diagnostics. Fixed aggregate keys; no row data in output."""

from collections import Counter
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from app.core.staff_models import AuditEvent, StaffSession
from app.modules.chat.models import ChatSession
from app.modules.notify.models import EmailOutbox
from app.modules.orders.cleanup_service import (
    BATCH_SIZE,
    MODELS,
    CleanupRow,
    RetentionReview,
    database_now,
    eligibility,
)
from app.modules.orders.models import Order
from app.modules.orders.privacy_service import PrivacyUnavailable


def retention_report(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = BATCH_SIZE,
    after_ids: dict[str, UUID] | None = None,
) -> dict[str, dict[str, int | bool | dict[str, int]]]:
    """Sample at most limit rows per category; explicitly report truncation.

    Reasons can overlap. This is an inventory requiring decisions, never a
    deletion permit. No cursor, dates, actions, references or identifiers leave
    the report. A truncated sample cannot claim to inventory the entire DB.
    """
    if not 1 <= limit <= BATCH_SIZE:
        raise ValueError("Invalid review limit")
    at = database_now(session, now)
    report: dict[str, dict[str, int | bool | dict[str, int]]] = {}
    for category, model in MODELS.items():
        query = select(model).order_by(model.id).limit(limit + 1)
        if after_ids is not None and category in after_ids:
            query = query.where(model.id > after_ids[category])
        if model is Order:
            query = query.options(
                load_only(
                    Order.id,
                    Order.status,
                    Order.consent_id,
                    Order.created_at,
                    Order.terminal_at,
                    Order.pii_erased_at,
                    raiseload=True,
                )
            )
        elif model is ChatSession:
            query = query.options(
                load_only(
                    ChatSession.id,
                    ChatSession.retention_version,
                    ChatSession.created_at,
                    raiseload=True,
                )
            )
        elif model is StaffSession:
            query = query.options(
                load_only(
                    StaffSession.id,
                    StaffSession.expires_at,
                    StaffSession.revoked_at,
                    raiseload=True,
                )
            )
        elif model is AuditEvent:
            query = query.options(
                load_only(
                    AuditEvent.id,
                    AuditEvent.action,
                    AuditEvent.at,
                    AuditEvent.result,
                    AuditEvent.entity_type,
                    raiseload=True,
                )
            )
        elif model is EmailOutbox:
            query = query.options(
                load_only(
                    EmailOutbox.id,
                    EmailOutbox.order_id,
                    EmailOutbox.status,
                    EmailOutbox.created_at,
                    EmailOutbox.sent_at,
                    EmailOutbox.failed_at,
                    EmailOutbox.lease_until,
                    raiseload=True,
                )
            )
        rows = cast(list[CleanupRow], list(session.scalars(query)))
        reasons: Counter[str] = Counter()
        reviewed = 0
        for row in rows[:limit]:
            findings: set[str] = set()
            if isinstance(row, Order) and row.status in {"closed", "cancelled"}:
                if (
                    row.terminal_at is None
                    or not row.created_at <= row.terminal_at <= at
                ):
                    findings.add("ORDER_TERMINAL_EVIDENCE")
            if isinstance(row, EmailOutbox) and row.status in {"sent", "failed"}:
                terminal = row.sent_at if row.status == "sent" else row.failed_at
                if terminal is None or not row.created_at <= terminal <= at:
                    findings.add("OUTBOX_TERMINAL_EVIDENCE")
            if isinstance(row, (AuditEvent, StaffSession)):
                findings.add("SERVICE_BINDING_UNKNOWN")
            try:
                _, _, review = eligibility(session, category, row, at)
                if review:
                    if isinstance(row, Order):
                        findings.add(
                            "OPEN_ORDER_REVIEW"
                            if row.status in {"new", "in_progress"}
                            else "ORDER_TERMINAL_EVIDENCE"
                            if row.terminal_at is None
                            or not row.created_at <= row.terminal_at <= at
                            else "LINKED_SNAPSHOTS_LEGAL_DECISION"
                        )
                    elif isinstance(row, EmailOutbox):
                        findings.add("OUTBOX_PENDING_REVIEW")
            except RetentionReview as error:
                findings.add(error.reason)
            except PrivacyUnavailable:
                findings.add("POLICY_INVALID")
            if findings:
                reviewed += 1
                reasons.update(findings)
        report[category] = {
            "inspected": min(len(rows), limit),
            "truncated": len(rows) > limit,
            "manual_review": reviewed,
            "reasons": dict(sorted(reasons.items())),
        }
        if after_ids is not None and rows:
            after_ids[category] = rows[min(len(rows), limit) - 1].id
    return report


def aggregate_retention_report(
    session: Session,
    *,
    limit: int = BATCH_SIZE,
    max_batches: int = 10,
) -> dict[str, dict[str, int | bool | dict[str, int]]]:
    """Bounded keyset pages in one caller-owned snapshot; cursors stay private."""
    if not 1 <= max_batches <= 10:
        raise ValueError("Invalid review batch count")
    after_ids: dict[str, UUID] = {}
    at = database_now(session)
    totals: dict[str, dict[str, int | bool | dict[str, int]]] = {}
    for _ in range(max_batches):
        page = retention_report(session, now=at, limit=limit, after_ids=after_ids)
        for category, counts in page.items():
            previous = totals.setdefault(
                category,
                {
                    "inspected": 0,
                    "manual_review": 0,
                    "truncated": False,
                    "reasons": {},
                },
            )
            for field in ("inspected", "manual_review"):
                previous[field] = cast(int, previous[field]) + cast(int, counts[field])
            previous["truncated"] = counts["truncated"]
            reasons = Counter(cast(dict[str, int], previous["reasons"]))
            reasons.update(cast(dict[str, int], counts["reasons"]))
            previous["reasons"] = dict(sorted(reasons.items()))
        if not any(counts["truncated"] for counts in page.values()):
            break
    return totals
