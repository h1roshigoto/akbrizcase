"""Isolated PostgreSQL 16 acceptance for the global provider budget."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.core.db import Database, DatabaseSettings
from app.modules.llm import LLMError, PostgresBudgetLedger, budget
from app.modules.llm.budget import BudgetAttempt

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime(databases: tuple[Database, Database]) -> Database:
    database, _ = databases
    with database.unit_of_work() as session:
        session.execute(text("DELETE FROM app.llm_budget_attempts"))
    return database


@pytest.mark.parametrize(
    ("daily_tokens", "rpm", "reason"),
    [(60, 10, "LLM_BUDGET_EXCEEDED"), (1000, 1, "LLM_RATE_LIMITED")],
)
def test_shared_limits_across_independent_connections(
    runtime: Database, daily_tokens: int, rpm: int, reason: str
) -> None:
    second = Database(DatabaseSettings.from_environment())
    barrier = Barrier(2)

    def reserve(database: Database) -> UUID | str:
        ledger = PostgresBudgetLedger(
            database, daily_token_budget=daily_tokens, max_requests_per_minute=rpm
        )
        barrier.wait(timeout=10)
        try:
            return ledger.reserve(60)
        except LLMError as error:
            return error.code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, (runtime, second)))
        assert outcomes.count(reason) == 1
        winner = next(item for item in outcomes if isinstance(item, UUID))
        with runtime.unit_of_work() as session:
            assert session.scalars(select(BudgetAttempt.id)).all() == [winner]
    finally:
        second.close()


def test_retry_safe_settlement_obeys_original_utc_day(
    runtime: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 2, 1, 23, 59, 59, tzinfo=UTC)
    monkeypatch.setattr(budget, "_database_now", lambda session: now)
    ledger = PostgresBudgetLedger(
        runtime, daily_token_budget=100, max_requests_per_minute=2
    )
    previous = ledger.reserve(100)
    now += timedelta(seconds=2)
    current = ledger.reserve(100)
    with pytest.raises(LLMError, match="LLM_RATE_LIMITED"):
        ledger.reserve(1)
    ledger.record_usage(previous, 5)
    ledger.record_usage(previous, 0)
    with runtime.unit_of_work() as session:
        old = session.get(BudgetAttempt, previous)
        new = session.get(BudgetAttempt, current)
        assert old is not None and (old.charged_tokens, old.settled) == (5, True)
        assert new is not None and (new.charged_tokens, new.settled) == (100, False)
    now += timedelta(minutes=1)
    with pytest.raises(LLMError, match="LLM_BUDGET_EXCEEDED"):
        ledger.reserve(1)
    ledger.record_usage(current, 30)
    ledger.reserve(70)
    with pytest.raises(LLMError, match="LLM_STORAGE_ERROR"):
        ledger.record_usage(uuid4(), 0)


def test_storage_contention_fails_closed(runtime: Database) -> None:
    ledger = PostgresBudgetLedger(
        runtime, daily_token_budget=100, max_requests_per_minute=2
    )
    with runtime.unit_of_work() as session:
        budget._lock_budget(session)
        with pytest.raises(LLMError, match="LLM_STORAGE_ERROR"):
            ledger.reserve(1)
    with runtime.unit_of_work() as session:
        assert session.scalars(select(BudgetAttempt.id)).all() == []
    ledger.reserve(100)
