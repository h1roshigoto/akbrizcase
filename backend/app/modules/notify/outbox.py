"""Short PostgreSQL transitions; callers own the outer Unit of Work."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.modules.notify.models import EmailOutbox
from app.modules.orders.models import Order

MAX_ATTEMPTS = 5
LEASE = timedelta(minutes=2)
RETRY = tuple(timedelta(minutes=n) for n in (5, 30, 120, 720))
ERROR_CODES = frozenset(
    {
        "SMTP_TIMEOUT",
        "SMTP_TLS",
        "SMTP_AUTH",
        "SMTP_REJECTED",
        "SMTP_UNAVAILABLE",
        "SEND_ERROR",
        "LEASE_EXPIRED",
    }
)


@dataclass(frozen=True)
class Claim:
    id: UUID
    order_id: UUID
    number: int
    token: UUID
    attempts: int


def database_now(session: Session) -> datetime:
    return session.execute(select(func.clock_timestamp())).scalar_one()  # type: ignore[no-any-return]


def claim_due(session: Session) -> Claim | None:
    row = session.scalar(
        select(EmailOutbox)
        .where(
            EmailOutbox.status == "pending",
            EmailOutbox.attempts < MAX_ATTEMPTS,
            EmailOutbox.next_attempt_at <= func.clock_timestamp(),
        )
        .order_by(EmailOutbox.next_attempt_at, EmailOutbox.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    # Only the public number is read; no order/contact ORM object is loaded.
    number = session.execute(
        select(Order.number).where(Order.id == row.order_id)
    ).scalar_one()
    row.status = "sending"
    row.attempts += 1
    row.lease_token = uuid4()
    row.lease_until = database_now(session) + LEASE
    session.flush()
    return Claim(row.id, row.order_id, number, row.lease_token, row.attempts)


def finish_success(session: Session, claim: Claim) -> bool:
    result = session.execute(
        update(EmailOutbox)
        .where(
            EmailOutbox.id == claim.id,
            EmailOutbox.status == "sending",
            EmailOutbox.lease_token == claim.token,
        )
        .values(
            status="sent",
            sent_at=func.clock_timestamp(),
            failed_at=None,
            lease_token=None,
            lease_until=None,
            last_error_code=None,
        )
        .returning(EmailOutbox.id)
    )
    return result.scalar_one_or_none() is not None


def _failed(row: EmailOutbox, now: datetime, code: str) -> None:
    row.status = "failed" if row.attempts >= MAX_ATTEMPTS else "pending"
    row.failed_at = now if row.status == "failed" else None
    if row.status == "pending":
        row.next_attempt_at = now + RETRY[max(0, row.attempts - 1)]
    row.lease_token = None
    row.lease_until = None
    row.last_error_code = code


def finish_failure(session: Session, claim: Claim, code: str) -> bool:
    if code not in ERROR_CODES:
        code = "SEND_ERROR"
    row = session.scalar(
        select(EmailOutbox)
        .where(
            EmailOutbox.id == claim.id,
            EmailOutbox.status == "sending",
            EmailOutbox.lease_token == claim.token,
        )
        .with_for_update()
    )
    if row is None:
        return False
    _failed(row, database_now(session), code)
    session.flush()
    return True


def recover_expired(session: Session, *, limit: int = 100) -> int:
    rows = list(
        session.scalars(
            select(EmailOutbox)
            .where(
                EmailOutbox.status == "sending",
                EmailOutbox.lease_until <= func.clock_timestamp(),
            )
            .order_by(EmailOutbox.lease_until, EmailOutbox.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    )
    now = database_now(session)
    for row in rows:
        # Recovery consumes no new attempt. Retry delay starts at recovery.
        _failed(row, now, "LEASE_EXPIRED")
    session.flush()
    return len(rows)
