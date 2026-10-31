"""Protected synthetic audit maintenance; never used by the ordinary worker."""

from collections import Counter
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.db import Database
from app.modules.orders.cleanup_service import (
    BATCH_SIZE,
    pending_commands,
    prepare_batch,
)
from app.modules.orders.deletion_journal import JournalSink
from app.modules.orders.deletion_reconciliation import JournalState, reconcile_command
from app.modules.orders.maintenance_authority import (
    authorize_maintenance as authorize_maintenance,
)


def audit_maintenance_tick(
    database: Database,
    sink: JournalSink,
    *,
    login: str,
    password: str,
    now: datetime | None = None,
    limit: int = BATCH_SIZE,
    stopped: Callable[[], bool] = lambda: False,
) -> dict[str, int]:
    """One bounded invocation; committed PREPARED, external I/O, separate apply.

    No policy override, decision UUID, restore import, scheduler or production
    sink. Existing unbound audit rows require review until a binding is approved.
    Credentials are only held by this protected process and rechecked per UoW.
    """
    if not 1 <= limit <= BATCH_SIZE:
        raise ValueError("Invalid maintenance limit")
    counts: Counter[str] = Counter()

    def authorize(session: Session) -> bool:
        if stopped():
            return False
        return (
            authorize_maintenance(
                session, login, password, action="privacy.audit_maintenance"
            )
            == "SUCCESS"
        )

    if stopped():
        return {"STOPPED": 1}
    with database.unit_of_work() as session:
        if not authorize(session):
            return {"AUTHORITY_DENIED": 1}
        scan = prepare_batch(session, "audit_delete", now=now, limit=limit)
    counts.update({"prepared": scan.processed, "manual_review": scan.manual_review})
    if stopped():
        return dict(counts)
    with database.unit_of_work() as session:
        if not authorize(session):
            counts["AUTHORITY_DENIED"] += 1
            return dict(counts)
        commands, synthetic = pending_commands(session, limit=limit, audit_only=True)
    if not synthetic:
        return {"REVIEW_REQUIRED": 1}
    for command in commands:
        if stopped():
            break
        result = reconcile_command(
            database, command, sink, execute=True, now=now, authorize_audit=authorize
        )
        counts[str(result.state)] += 1
        if result.state == JournalState.REVIEW_REQUIRED:
            counts["manual_review"] += 1
    return dict(counts)
