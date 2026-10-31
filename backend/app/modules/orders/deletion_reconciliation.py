"""Synthetic protocol runner. Every sink call is outside a database UoW.

This reconciles one known command, not a complete restore manifest. No production
provider, sink enumeration, terminal acknowledgement or pruning is implemented.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.db import Database
from app.modules.orders.cleanup_service import (
    MODELS,
    apply_command,
    database_now,
    policy_for,
    try_lock,
)
from app.modules.orders.deletion_journal import (
    DeletionCommand,
    JournalConflict,
    JournalSink,
    JournalUnavailable,
    command_for,
    read_witness,
    verify_witness,
)
from app.modules.orders.deletion_models import DeletionEntry
from app.modules.orders.erasure_service import pii_erasure_complete
from app.modules.orders.models import Consent, Order
from app.modules.orders.privacy_service import PrivacyUnavailable, synthetic_database


class JournalState(StrEnum):
    PREPARED = "PREPARED"
    WITNESSED = "WITNESSED"
    APPLIED = "APPLIED"
    LOCAL_MISSING = "LOCAL_MISSING"
    SINK_UNAVAILABLE = "SINK_UNAVAILABLE"
    CONFLICT = "CONFLICT"
    UNCONFIRMED = "UNCONFIRMED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True)
class Reconciliation:
    state: JournalState
    # True only when a new read confirmed local applied_at AND its postcondition.
    # This is not an external durable terminal acknowledgement or a prune permit.
    commit_confirmed: bool = False


def postcondition(session: Session, command: DeletionCommand) -> bool:
    if command.operation == "order_delete":
        return (
            session.get(Order, command.object_id) is None
            and session.get(Consent, command.consent_id) is None
        )
    if command.operation == "order_pii":
        return pii_erasure_complete(session, command.object_id)
    return session.get(MODELS[command.operation], command.object_id) is None


def local_state(session: Session, command: DeletionCommand) -> JournalState:
    if not synthetic_database(session):
        raise JournalUnavailable("Independent journal storage required")
    entry = session.get(DeletionEntry, command.id)
    if entry is None:
        # A restored DB can have another operation UUID for the same target.
        # Never replace either command or silently attach the external witness.
        collision = session.scalar(
            select(DeletionEntry.id).where(
                DeletionEntry.operation == command.operation,
                DeletionEntry.object_id == command.object_id,
            )
        )
        return (
            JournalState.CONFLICT
            if collision is not None
            else JournalState.LOCAL_MISSING
        )
    if command_for(entry).canonical() != command.canonical():
        return JournalState.CONFLICT
    if entry.applied_at is None:
        return JournalState.PREPARED
    return (
        JournalState.APPLIED
        if postcondition(session, command)
        else JournalState.CONFLICT
    )


def restore_prepared(
    session: Session, command: DeletionCommand, now: datetime | None
) -> None:
    """Explicit isolated restore seam; never infer missing commands from targets.

    Caller has just verified the external witness. Preserve the exact UUID and
    all canonical fields, validate immutable policy evidence, commit PREPARED
    before any mutation. Current application roles cannot import a manual delete.
    """
    if (
        not synthetic_database(session)
        or session.scalar(select(func.current_user())) != "akbrizcase_migrator"
    ):
        raise JournalUnavailable("Protected synthetic restore required")
    at = database_now(session, now)
    bundle = policy_for(session, command.journal_version, at)
    if (
        command.retention_days != bundle.retention.deletion_journal_days
        or at < command.created_at
    ):
        raise JournalUnavailable("Restore policy evidence mismatch")
    if not try_lock(session):
        return
    session.execute(
        insert(DeletionEntry)
        .values(
            **{
                name: getattr(command, name)
                for name in (
                    "id",
                    "format",
                    "operation",
                    "object_id",
                    "consent_id",
                    "decision_id",
                    "policy_version",
                    "journal_policy_version",
                    "created_at",
                    "retain_until",
                )
            }
        )
        .on_conflict_do_nothing()
    )


def reconcile_command(
    database: Database,
    command: DeletionCommand,
    sink: JournalSink,
    *,
    execute: bool = False,
    restore_missing: bool = False,
    now: datetime | None = None,
    authorize_manual: Callable[[Session], bool] | None = None,
) -> Reconciliation:
    """Inspect/retry a known command, including an unknown commit outcome.

    execute exports only committed PREPARED commands. restore_missing is opt-in,
    requires an existing witness and migrator; full deletion also reauthenticates.
    Reapplying a restored target never trusts a previous completion report.
    A failed commit is followed by a fresh read; if that too fails, UNCONFIRMED
    explicitly leaves the outcome unknown. No boolean mutation result is success.
    """
    try:
        with database.unit_of_work() as session:
            state = local_state(session, command)
        if state == JournalState.CONFLICT:
            return Reconciliation(state)
        try:
            proof = read_witness(sink, command, synthetic=True)
            if proof is None:
                if state == JournalState.APPLIED:
                    return Reconciliation(JournalState.UNCONFIRMED)
                if state == JournalState.LOCAL_MISSING or not execute:
                    return Reconciliation(state)
                receipt = sink.export(command, synthetic=True)
                proof = verify_witness(sink, command, receipt, synthetic=True)
        except JournalConflict:
            return Reconciliation(JournalState.CONFLICT)
        except (JournalUnavailable, OSError):
            return Reconciliation(JournalState.SINK_UNAVAILABLE)
        if state == JournalState.APPLIED:
            return Reconciliation(state, commit_confirmed=True)
        if state == JournalState.LOCAL_MISSING:
            if not (execute and restore_missing):
                return Reconciliation(state)
            with database.unit_of_work() as session:
                if command.operation == "order_delete" and (
                    authorize_manual is None or not authorize_manual(session)
                ):
                    return Reconciliation(JournalState.REVIEW_REQUIRED)
                restore_prepared(session, command, now)
            with database.unit_of_work() as session:
                state = local_state(session, command)
            if state != JournalState.PREPARED:
                return Reconciliation(state)
        if not execute:
            return Reconciliation(JournalState.WITNESSED)
        try:
            with database.unit_of_work() as session:
                manual = False
                if command.operation == "order_delete":
                    if authorize_manual is None or not authorize_manual(session):
                        return Reconciliation(JournalState.REVIEW_REQUIRED)
                    manual = True
                apply_command(
                    session, command, proof, now=now, manual_authorized=manual
                )
        except (SQLAlchemyError, OSError):
            # Rollback may itself fail, or the server may have committed already.
            # The fresh UoW below is the only authority for the local outcome.
            pass
        with database.unit_of_work() as session:
            state = local_state(session, command)
        if state == JournalState.APPLIED:
            return Reconciliation(state, commit_confirmed=True)
        return Reconciliation(
            JournalState.WITNESSED if state == JournalState.PREPARED else state
        )
    except (JournalUnavailable, PrivacyUnavailable):
        return Reconciliation(JournalState.REVIEW_REQUIRED)
    except (SQLAlchemyError, OSError):
        return Reconciliation(JournalState.UNCONFIRMED)
