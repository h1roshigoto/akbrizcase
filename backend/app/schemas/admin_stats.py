"""Closed aggregate projections; no personal records or arbitrary dimensions."""

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Self

from pydantic import Field, model_validator

from app.schemas.common import DTO

Count = Annotated[int, Field(ge=0)]


class StatsPeriod(DTO):
    start: date
    end: date

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if not 0 <= (self.end - self.start).days < 90 or self.end == date.max:
            raise ValueError("Invalid statistics period")
        return self

    @property
    def start_utc(self) -> datetime:
        return datetime.combine(self.start, time.min, UTC)

    @property
    def stop_utc(self) -> datetime:
        return datetime.combine(self.end + timedelta(days=1), time.min, UTC)

    @classmethod
    def from_query(cls, pairs: list[tuple[str, str]]) -> Self:
        # Preserve multiplicity until validation; dict() alone loses duplicates.
        if not pairs:
            today = datetime.now(UTC).date()
            return cls(start=today - timedelta(days=29), end=today)
        if len(pairs) != 2 or {key for key, _ in pairs} != {"from", "to"}:
            raise ValueError("Invalid statistics parameters")
        values = dict(pairs)
        if any(
            re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v) is None
            for v in values.values()
        ):
            raise ValueError("Invalid statistics dates")
        return cls(
            start=date.fromisoformat(values["from"]),
            end=date.fromisoformat(values["to"]),
        )


class OrderStats(DTO):
    total: Count
    commercial: Count
    support: Count
    new: Count
    in_progress: Count
    closed: Count
    cancelled: Count
    site: Count
    chat: Count
    chat_linked: Count


class SessionStats(DTO):
    total: Count
    diagnose: Count
    knowledge: Count
    commerce: Count
    resolved: Count
    handoff: Count
    unresolved: Count
    no_outcome: Count


class FeedbackStats(DTO):
    positive: Count
    negative: Count


class AdminStats(DTO):
    period: StatsPeriod
    orders: OrderStats
    sessions: SessionStats
    feedback: FeedbackStats
