"""Bounded deterministic scans and policy checks; caller owns every commit."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic
from typing import cast
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, load_only

from app.core.privacy import PrivacyBundle
from app.core.staff import lock_staff
from app.core.staff_models import AuditEvent, StaffSession
from app.modules.chat.models import ChatMessage, ChatSession
from app.modules.notify.models import EmailOutbox
from app.modules.orders.deletion_journal import (
    DeletionCommand,
    JournalUnavailable,
    VerifiedWitness,
    command_for,
    next_midnight,
)
from app.modules.orders.deletion_models import CleanupCursor, DeletionEntry
from app.modules.orders.erasure_service import (
    delete_order_graph,
    erase_order_pii,
    pii_erasure_complete,
)
from app.modules.orders.models import Consent, Order, OrderItem
from app.modules.orders.privacy_models import ConsentPublication
from app.modules.orders.privacy_service import (
    PrivacyUnavailable,
    synthetic_database,
    validated_publication,
)

CLEANUP_LOCK = 4038
BATCH_SIZE = 100
CATEGORIES = (
    "order_pii",
    "chat_delete",
    "outbox_delete",
    "staff_session_delete",
    "audit_delete",
)
TECHNICAL_AUDIT = frozenset(
    {"staff.login", "staff.logout", "staff.access", "staff.csrf"}
)
CleanupRow = Order | ChatSession | EmailOutbox | StaffSession | AuditEvent
MODELS: dict[str, type[CleanupRow]] = {
    "order_pii": Order,
    "chat_delete": ChatSession,
    "outbox_delete": EmailOutbox,
    "staff_session_delete": StaffSession,
    "audit_delete": AuditEvent,
}


@dataclass
class CleanupCounts:
    processed: int = 0
    skipped: int = 0
    errors: int = 0
    manual_review: int = 0


def database_now(session: Session, now: datetime | None = None) -> datetime:
    if now is not None:
        if now.utcoffset() != timedelta(0):
            raise ValueError("UTC clock required")
        return now
    return session.execute(select(func.clock_timestamp())).scalar_one()  # type: ignore[no-any-return]


def try_lock(session: Session) -> bool:
    return bool(
        session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": CLEANUP_LOCK}
        )
    )


def policy_for(session: Session, version: str | None, now: datetime) -> PrivacyBundle:
    if version is None:
        raise PrivacyUnavailable("Retention policy unbound")
    publication = session.get(ConsentPublication, version)
    if publication is None:
        raise PrivacyUnavailable("Retention policy missing")
    # Retirement and runtime file loss close collection, not prior obligations.
    return validated_publication(session, publication, now)[0]


def order_policy(session: Session, order: Order, now: datetime) -> PrivacyBundle:
    consent = session.get(Consent, order.consent_id)
    if consent is None:
        raise PrivacyUnavailable("Retention policy missing")
    bundle = policy_for(session, consent.text_version, now)
    publication = session.get(ConsentPublication, consent.text_version)
    assert publication is not None
    if publication.published_at > consent.granted_at or consent.granted_at > now:
        raise PrivacyUnavailable("Retention chronology requires review")
    return bundle


def shared_policy(session: Session, now: datetime) -> PrivacyBundle:
    versions = list(
        session.scalars(select(ConsentPublication.consent_version).limit(2))
    )
    if len(versions) != 1:
        raise PrivacyUnavailable("Unbound service retention requires review")
    return policy_for(session, versions[0], now)


def eligibility(
    session: Session,
    operation: str,
    row: Order | ChatSession | EmailOutbox | StaffSession | AuditEvent,
    now: datetime,
) -> tuple[PrivacyBundle, bool, bool]:
    """Return policy, due, manual-review. Never substitute updated_at for evidence."""
    if isinstance(row, Order):
        bundle = order_policy(session, row, now)
        if row.status not in {"closed", "cancelled"}:
            return (
                bundle,
                False,
                row.created_at + timedelta(days=bundle.retention.open_order_review_days)
                <= now,
            )
        if (
            row.terminal_at is None
            or row.terminal_at < row.created_at
            or row.terminal_at > now
        ):
            return bundle, False, True
        snapshots = bool(
            session.scalar(
                select(OrderItem.id).where(OrderItem.order_id == row.id).limit(1)
            )
        )
        review = False
        if snapshots:
            # Numbers, consent, history and external customer correspondence
            # keep these snapshots linkable. No unilateral anonymization scheme.
            review = (
                row.terminal_at
                + timedelta(days=bundle.retention.linked_snapshots_after_terminal_days)
                <= now
            )
        return (
            bundle,
            row.pii_erased_at is None
            and row.terminal_at
            + timedelta(days=bundle.retention.order_pii_after_terminal_days)
            <= now,
            review,
        )
    if isinstance(row, ChatSession):
        bundle = policy_for(session, row.retention_version, now)
        publication = session.get(ConsentPublication, row.retention_version)
        assert publication is not None
        if publication.published_at > row.created_at:
            raise PrivacyUnavailable("Chat retention chronology requires review")
        return (
            bundle,
            row.created_at + timedelta(days=bundle.retention.chat_storage_days) <= now,
            False,
        )
    if isinstance(row, EmailOutbox):
        order = session.get(Order, row.order_id)
        if order is None:
            raise PrivacyUnavailable("Outbox order missing")
        bundle = order_policy(session, order, now)
        if row.status == "sent":
            return (
                bundle,
                row.sent_at is not None
                and row.sent_at + timedelta(days=bundle.retention.outbox_sent_days)
                <= now,
                row.sent_at is None,
            )
        if row.status == "failed":
            return (
                bundle,
                row.failed_at is not None
                and row.failed_at + timedelta(days=bundle.retention.outbox_failed_days)
                <= now,
                row.failed_at is None,
            )
        review = row.created_at + timedelta(
            hours=bundle.retention.outbox_pending_review_hours
        ) <= now or (
            row.status == "sending"
            and row.lease_until is not None
            and row.lease_until <= now
        )
        return bundle, False, review
    bundle = shared_policy(session, now)
    if isinstance(row, StaffSession):
        return (
            bundle,
            row.expires_at <= now
            or (row.revoked_at is not None and row.revoked_at <= now),
            False,
        )
    if row.action not in TECHNICAL_AUDIT:
        return bundle, False, False
    return bundle, row.at + timedelta(days=bundle.retention.audit_days) <= now, False


def prepare_entry(
    session: Session,
    operation: str,
    object_id: UUID,
    bundle: PrivacyBundle,
    now: datetime,
    *,
    consent_id: UUID | None = None,
    decision_id: UUID | None = None,
) -> DeletionEntry:
    session.execute(
        insert(DeletionEntry)
        .values(
            format=2,
            operation=operation,
            object_id=object_id,
            policy_version=None
            if operation == "order_delete"
            else bundle.consent.version,
            journal_policy_version=bundle.consent.version,
            created_at=now,
            retain_until=now + timedelta(days=bundle.retention.deletion_journal_days),
            consent_id=consent_id,
            decision_id=decision_id,
        )
        .on_conflict_do_nothing(index_elements=["operation", "object_id"])
    )
    return session.scalars(
        select(DeletionEntry).where(
            DeletionEntry.operation == operation, DeletionEntry.object_id == object_id
        )
    ).one()


def prepare_batch(
    session: Session,
    category: str,
    *,
    now: datetime | None = None,
    limit: int = BATCH_SIZE,
) -> CleanupCounts:
    if category not in CATEGORIES or not 1 <= limit <= BATCH_SIZE:
        raise ValueError("Invalid cleanup batch")
    counts = CleanupCounts()
    deadline = monotonic() + 1.0
    if not try_lock(session):
        counts.skipped = 1
        return counts
    session.execute(text("SET LOCAL statement_timeout = '500ms'"))
    session.execute(text("SET LOCAL lock_timeout = '100ms'"))
    now = database_now(session, now)
    session.execute(
        insert(CleanupCursor)
        .values(category=category, next_run_at=now)
        .on_conflict_do_nothing()
    )
    cursor = session.scalars(
        select(CleanupCursor)
        .where(CleanupCursor.category == category)
        .with_for_update()
    ).one()
    if cursor.next_run_at > now:
        return counts
    model = MODELS[category]
    query = select(model).order_by(model.id).limit(limit)
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
    elif model is AuditEvent:
        query = query.options(
            load_only(AuditEvent.id, AuditEvent.action, AuditEvent.at, raiseload=True)
        )
    if cursor.after_id is not None:
        query = query.where(model.id > cursor.after_id)
    # Global xact lock serializes planners. Do not SKIP locked scan rows while
    # moving a keyset cursor, which could silently miss a retention candidate.
    # Mutation reacquires its individual FOR UPDATE SKIP LOCKED later.
    rows = cast(list[CleanupRow], list(session.scalars(query)))
    inspected = 0
    audit_privileged = (
        category != "audit_delete"
        or session.scalar(select(func.current_user())) == "akbrizcase_migrator"
    )
    for row in rows:
        if inspected and monotonic() >= deadline:
            break
        try:
            bundle, due, review = eligibility(session, category, row, now)
            if review:
                counts.manual_review += 1
            if due:
                if audit_privileged:
                    prepare_entry(session, category, row.id, bundle, now)
                    counts.processed += 1
                else:
                    counts.manual_review += 1
            else:
                counts.skipped += 1
        except PrivacyUnavailable:
            counts.manual_review += 1
        inspected += 1
        cursor.after_id = row.id
    if inspected == len(rows) and len(rows) < limit:
        cursor.after_id = None
        cursor.next_run_at = next_midnight(now)
    session.flush()
    return counts


def pending_commands(
    session: Session, *, limit: int = BATCH_SIZE
) -> tuple[list[DeletionCommand], bool]:
    if not 1 <= limit <= BATCH_SIZE:
        raise ValueError("Invalid journal batch")
    if not try_lock(session):
        return [], False
    now = database_now(session)
    session.execute(
        insert(CleanupCursor)
        .values(category="journal", next_run_at=now)
        .on_conflict_do_nothing()
    )
    cursor = session.scalars(
        select(CleanupCursor)
        .where(CleanupCursor.category == "journal")
        .with_for_update()
    ).one()
    query = (
        select(DeletionEntry)
        .where(
            DeletionEntry.applied_at.is_(None),
            DeletionEntry.operation != "order_delete",
        )
        .order_by(DeletionEntry.id)
        .limit(limit)
    )
    if cursor.after_id is not None:
        query = query.where(DeletionEntry.id > cursor.after_id)
    rows = list(session.scalars(query))
    cursor.after_id = rows[-1].id if len(rows) == limit else None
    return [command_for(row) for row in rows], synthetic_database(session)


def apply_command(
    session: Session,
    command: DeletionCommand,
    witness: VerifiedWitness | bytes,
    *,
    now: datetime | None = None,
    manual_authorized: bool = False,
) -> bool:
    if (
        not isinstance(witness, VerifiedWitness)
        or not witness.matches(command)
        or not synthetic_database(session)
    ):
        raise JournalUnavailable("Independent journal storage required")
    # Staff authority is always first in the global lock order. A session
    # resolver cannot race session cleanup/revocation and reacquire authority.
    if command.operation == "staff_session_delete":
        lock_staff(session)
    if not try_lock(session):
        return False
    now = database_now(session, now)
    entry = session.scalar(
        select(DeletionEntry)
        .where(DeletionEntry.id == command.id)
        .with_for_update(skip_locked=True)
    )
    if entry is None or command_for(entry) != command or entry.applied_at is not None:
        return False
    journal_bundle = policy_for(session, command.journal_version, now)
    if command.retention_days != journal_bundle.retention.deletion_journal_days:
        raise JournalUnavailable("Journal retention approval mismatch")
    if now < entry.created_at:
        raise JournalUnavailable("Journal clock requires review")
    if command.operation == "order_delete":
        if (
            not manual_authorized
            or session.scalar(select(func.current_user())) != "akbrizcase_migrator"
            or command.consent_id is None
        ):
            raise JournalUnavailable("Protected operator authorization required")
        changed = delete_order_graph(session, command.object_id, command.consent_id)
        if not changed and session.get(Consent, command.consent_id) is not None:
            raise JournalUnavailable("Deletion postcondition requires review")
    else:
        model = MODELS[command.operation]
        row = cast(
            CleanupRow | None,
            session.scalar(
                select(model)
                .where(model.id == entry.object_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            ),
        )
        if row is None:
            # Could be locked, not deleted. Leave pending for an exact retry.
            if (
                session.scalar(select(model.id).where(model.id == entry.object_id))
                is None
            ):
                entry.applied_at = now
                session.flush()
            return False
        current, due, review = eligibility(session, command.operation, row, now)
        if (
            isinstance(row, Order)
            and row.pii_erased_at is not None
            and current.consent.version == entry.policy_version
        ):
            # An exact restore/retry can find an already erased target without
            # this command's local applied_at. Verify the complete postcondition.
            if pii_erasure_complete(session, command.object_id):
                entry.applied_at = now
                session.flush()
            return False
        if not due or current.consent.version != entry.policy_version:
            return False
        if command.operation == "order_pii":
            changed = erase_order_pii(session, row.id, now)
        elif isinstance(row, ChatSession):
            # One large conversation must not turn a bounded session batch into
            # an unlimited message cascade. The witnessed command authorizes
            # the entire session; incomplete chunks retain the pending command.
            ids = (
                select(ChatMessage.id)
                .where(ChatMessage.session_id == row.id)
                .order_by(ChatMessage.id)
                .limit(BATCH_SIZE)
            )
            session.execute(delete(ChatMessage).where(ChatMessage.id.in_(ids)))
            if (
                session.scalar(
                    select(ChatMessage.id)
                    .where(ChatMessage.session_id == row.id)
                    .limit(1)
                )
                is not None
            ):
                session.flush()
                return True
            session.delete(row)
            changed = True
        elif isinstance(row, AuditEvent):
            # Runtime remains append-only. Do not grant audit DELETE to API.
            if session.scalar(select(func.current_user())) != "akbrizcase_migrator":
                return False
            session.delete(row)
            changed = True
        else:
            session.delete(row)
            changed = True
    entry.applied_at = now
    session.flush()
    return changed
