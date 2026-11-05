"""One bounded scan per category/tick; no transaction spans journal I/O."""

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from app.core.db import Database
from app.core.observability import emit
from app.core.rate_limits import cleanup_buckets
from app.modules.orders.cleanup_service import (
    CATEGORIES,
    pending_commands,
    prepare_batch,
)
from app.modules.orders.deletion_journal import JournalSink, SyntheticJournal
from app.modules.orders.deletion_reconciliation import JournalState, reconcile_command


def cleanup_tick(
    database: Database,
    *,
    now: datetime | None = None,
    journal: JournalSink | None = None,
    stopped: Callable[[], bool] = lambda: False,
) -> None:
    for category in CATEGORIES:
        if category == "audit_delete":
            # Only the protected maintenance process owns the audit scan/cursor.
            continue
        if stopped():
            return
        try:
            with database.unit_of_work() as session:
                counts = prepare_batch(session, category, now=now)
            if counts.processed or counts.manual_review:
                emit(
                    "CLEANUP",
                    processed=counts.processed,
                    skipped=counts.skipped,
                    errors=counts.errors,
                    manual_review=counts.manual_review,
                )
        except Exception:
            emit("CLEANUP_SCAN_FAILED")
    try:
        with database.unit_of_work() as session:
            commands, synthetic = pending_commands(session)
        if not commands:
            return
        if journal is None:
            directory = os.environ.get("SYNTHETIC_DELETION_JOURNAL_DIR")
            if not directory or not synthetic:
                emit("CLEANUP_JOURNAL_REQUIRED", manual_review=len(commands))
                return
            journal = SyntheticJournal(Path(directory))
        for command in commands:
            if stopped():
                return
            try:
                result = reconcile_command(
                    database, command, journal, execute=True, now=now
                )
                if result.state == JournalState.APPLIED and result.commit_confirmed:
                    emit("CLEANUP_APPLIED", processed=1)
                else:
                    emit("CLEANUP_RECONCILIATION", manual_review=1)
            except Exception:
                emit("CLEANUP_APPLY_FAILED", errors=1)
    except Exception:
        emit("CLEANUP_JOURNAL_FAILED errors=1")


def cleanup_rate_limits(database: Database) -> None:
    try:
        with database.unit_of_work() as session:
            cleanup_buckets(session)
    except Exception:
        emit("RATE_LIMIT_CLEANUP_FAILED")
