"""One bounded scan per category/tick; no transaction spans journal I/O."""

import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from app.core.db import Database
from app.modules.orders.cleanup_service import (
    CATEGORIES,
    apply_command,
    pending_commands,
    prepare_batch,
)
from app.modules.orders.deletion_journal import SyntheticJournal


def cleanup_tick(
    database: Database,
    *,
    now: datetime | None = None,
    journal: SyntheticJournal | None = None,
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
                witness = journal.witness(command, synthetic=synthetic)
                with database.unit_of_work() as session:
                    changed = apply_command(session, command, witness, now=now)
                if changed:
                    logging.info("CLEANUP_APPLIED processed=1")
            except Exception:
                logging.error("CLEANUP_APPLY_FAILED errors=1")
    except Exception:
        logging.error("CLEANUP_JOURNAL_FAILED errors=1")
