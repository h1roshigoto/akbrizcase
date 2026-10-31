"""One bounded scan per category/tick; no transaction spans journal I/O."""

import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from app.core.db import Database
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
        if stopped():
            return
        try:
            with database.unit_of_work() as session:
                counts = prepare_batch(session, category, now=now)
            if counts.processed or counts.manual_review:
                logging.info(
                    "CLEANUP category=%s prepared=%d skipped=%d "
                    "errors=%d manual_review=%d",
                    category,
                    counts.processed,
                    counts.skipped,
                    counts.errors,
                    counts.manual_review,
                )
        except Exception:
            logging.error("CLEANUP_SCAN_FAILED category=%s", category)
    try:
        with database.unit_of_work() as session:
            commands, synthetic = pending_commands(session)
        if not commands:
            return
        if journal is None:
            directory = os.environ.get("SYNTHETIC_DELETION_JOURNAL_DIR")
            if not directory or not synthetic:
                logging.error(
                    "CLEANUP_JOURNAL_REQUIRED manual_review=%d", len(commands)
                )
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
                    logging.info("CLEANUP_APPLIED processed=1")
                else:
                    logging.info("CLEANUP_RECONCILIATION state=%s", result.state)
            except Exception:
                logging.error("CLEANUP_APPLY_FAILED errors=1")
    except Exception:
        logging.error("CLEANUP_JOURNAL_FAILED errors=1")
