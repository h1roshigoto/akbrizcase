"""Shared budget reservations for one deployment's GigaChat completions."""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from threading import Lock
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, Index, delete, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.db import Database
from app.core.db_types import Base, utc_now
from app.modules.llm.schemas import LLMError

MINUTE = timedelta(minutes=1)
RETENTION = timedelta(days=2)
MAX_TOKEN_COUNT = 2_147_483_647


class BudgetLedger(Protocol):
    def reserve(self, estimated_tokens: int) -> UUID: ...

    def record_usage(self, reservation: UUID, actual_tokens: int) -> None: ...


class BudgetAttempt(Base):
    __tablename__ = "llm_budget_attempts"
    __table_args__ = (
        CheckConstraint("charged_tokens >= 0", name="charge"),
        Index("ix_llm_budget_attempts_reserved_at", "reserved_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    reserved_at: Mapped[datetime]
    charged_tokens: Mapped[int] = mapped_column(BigInteger)
    settled: Mapped[bool]


def _validate_tokens(value: int) -> None:
    if type(value) is not int or not 0 <= value <= MAX_TOKEN_COUNT:
        raise LLMError("LLM_CONFIG_INVALID")


def _day_start(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(UTC).date(), time.min, tzinfo=UTC)


def _check_budget(
    used: int, requests: int, estimated: int, daily_budget: int, max_rpm: int
) -> None:
    if requests >= max_rpm:
        raise LLMError("LLM_RATE_LIMITED")
    if used + estimated > daily_budget:
        raise LLMError("LLM_BUDGET_EXCEEDED")


def _lock_budget(session: Session) -> None:
    session.execute(text("SET LOCAL lock_timeout = '2s'"))
    session.execute(text("SET LOCAL statement_timeout = '2s'"))
    session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended('akbrizcase:llm:budget:v1', 0))"
        )
    )


def _database_now(session: Session) -> datetime:
    now = session.scalar(select(func.clock_timestamp()))
    assert isinstance(now, datetime)
    return now


class PostgresBudgetLedger:
    """Serialize short reserve and settlement transactions across API workers."""

    def __init__(
        self,
        database: Database,
        *,
        daily_token_budget: int,
        max_requests_per_minute: int,
    ) -> None:
        self.database = database
        self.daily_token_budget = daily_token_budget
        self.max_requests_per_minute = max_requests_per_minute

    def reserve(self, estimated_tokens: int) -> UUID:
        _validate_tokens(estimated_tokens)
        if estimated_tokens == 0:
            raise LLMError("LLM_CONFIG_INVALID")
        identifier = uuid4()
        try:
            with self.database.unit_of_work() as session:
                _lock_budget(session)
                now = _database_now(session)
                day = _day_start(now)
                used = session.scalar(
                    select(
                        func.coalesce(func.sum(BudgetAttempt.charged_tokens), 0)
                    ).where(BudgetAttempt.reserved_at >= day)
                )
                requests = session.scalar(
                    select(func.count())
                    .select_from(BudgetAttempt)
                    .where(BudgetAttempt.reserved_at > now - MINUTE)
                )
                _check_budget(
                    int(used or 0),
                    int(requests or 0),
                    estimated_tokens,
                    self.daily_token_budget,
                    self.max_requests_per_minute,
                )
                session.execute(
                    delete(BudgetAttempt).where(
                        BudgetAttempt.reserved_at < day - RETENTION
                    )
                )
                session.add(
                    BudgetAttempt(
                        id=identifier,
                        reserved_at=now,
                        charged_tokens=estimated_tokens,
                        settled=False,
                    )
                )
        except (OSError, SQLAlchemyError, ValueError):
            raise LLMError("LLM_STORAGE_ERROR") from None
        return identifier

    def record_usage(self, reservation: UUID, actual_tokens: int) -> None:
        _validate_tokens(actual_tokens)
        try:
            with self.database.unit_of_work() as session:
                _lock_budget(session)
                attempt = session.get(BudgetAttempt, reservation)
                if attempt is None:
                    raise LLMError("LLM_STORAGE_ERROR")
                if not attempt.settled:
                    attempt.charged_tokens = actual_tokens
                    attempt.settled = True
        except (OSError, SQLAlchemyError, ValueError):
            raise LLMError("LLM_STORAGE_ERROR") from None


@dataclass
class _Attempt:
    reserved_at: datetime
    charged_tokens: int
    settled: bool = False


class InMemoryBudgetLedger:
    """Process-local ledger for provider tests and isolated evaluation."""

    def __init__(
        self, *, daily_token_budget: int, max_requests_per_minute: int
    ) -> None:
        self.daily_token_budget = daily_token_budget
        self.max_requests_per_minute = max_requests_per_minute
        self._lock = Lock()
        self._attempts: dict[UUID, _Attempt] = {}

    def reserve(self, estimated_tokens: int) -> UUID:
        _validate_tokens(estimated_tokens)
        if estimated_tokens == 0:
            raise LLMError("LLM_CONFIG_INVALID")
        with self._lock:
            now = utc_now()
            day = _day_start(now)
            self._attempts = {
                identifier: attempt
                for identifier, attempt in self._attempts.items()
                if attempt.reserved_at >= day - RETENTION
            }
            used = sum(
                attempt.charged_tokens
                for attempt in self._attempts.values()
                if attempt.reserved_at >= day
            )
            requests = sum(
                attempt.reserved_at > now - MINUTE
                for attempt in self._attempts.values()
            )
            _check_budget(
                used,
                requests,
                estimated_tokens,
                self.daily_token_budget,
                self.max_requests_per_minute,
            )
            identifier = uuid4()
            self._attempts[identifier] = _Attempt(now, estimated_tokens)
            return identifier

    def record_usage(self, reservation: UUID, actual_tokens: int) -> None:
        _validate_tokens(actual_tokens)
        with self._lock:
            attempt = self._attempts.get(reservation)
            if attempt is None:
                raise LLMError("LLM_STORAGE_ERROR")
            if not attempt.settled:
                attempt.charged_tokens = actual_tokens
                attempt.settled = True
