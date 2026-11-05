"""Transaction-scoped PostgreSQL counters. A rollback never consumes quota."""

import hashlib
import hmac
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import CheckConstraint, Index, String, select, text
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.db_types import Base
from app.modules.orders.security import OrderSecurity


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"
    __table_args__ = (
        CheckConstraint("scope IN ('session','order','message')", name="scope"),
        CheckConstraint("subject_hash ~ '^[0-9a-f]{64}$'", name="subject_hash"),
        CheckConstraint("used BETWEEN 1 AND 30", name="used"),
        CheckConstraint(
            "expires_at > started_at AND expires_at <= started_at + interval '1 hour'",
            name="lifetime",
        ),
        Index("ix_rate_limit_buckets_expires_at", "expires_at"),
    )
    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    subject_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    used: Mapped[int]
    started_at: Mapped[datetime]
    expires_at: Mapped[datetime]


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__("Rate limit exceeded")


@dataclass(frozen=True)
class IpSecurity:
    keyring: OrderSecurity = field(repr=False)

    @classmethod
    def from_environment(cls) -> "IpSecurity | None":
        ring = OrderSecurity.from_environment("IP_HMAC_SECRET_FILE")
        return cls(ring) if ring else None

    def subjects(self, scope: str, address: str, epoch: int) -> tuple[str, ...]:
        # Separate domains prevent matching IPs across quotas or order HMACs.
        return tuple(
            sorted(
                hmac.new(
                    key,
                    f"akbrizcase:ip:v1:{scope}:{hour}:{address}".encode("ascii"),
                    hashlib.sha256,
                ).hexdigest()
                for key in (self.keyring.secret, *self.keyring.previous)
                for hour in (epoch - 1, epoch)
            )
        )


def check_limit(
    session: Session,
    scope: str,
    subjects: tuple[str, ...],
    *,
    consume: bool = True,
) -> None:
    """First committed success opens a fixed window; failed writes cost nothing.

    All retained rotation aliases share the maximum current counter/window.
    Sorted transaction locks serialize aliases across rolling API restarts. SQL
    UPSERT plus those locks owns the whole decision, across independent processes.
    Read-only prechecks are advisory and never allocate a bucket; consumption
    always rechecks under locks after the complete creation write set.
    """
    limit, seconds = {"session": (10, 3600), "order": (5, 3600), "message": (30, 60)}[
        scope
    ]
    if consume:
        lock_subjects(session, scope, subjects)
    now = session.scalar(select(text("clock_timestamp()")))
    assert isinstance(now, datetime)
    buckets = list(
        session.scalars(
            select(RateLimitBucket)
            .where(
                RateLimitBucket.scope == scope,
                RateLimitBucket.subject_hash.in_(subjects),
                RateLimitBucket.expires_at > now,
            )
            .execution_options(populate_existing=True)
        )
    )
    used = max((bucket.used for bucket in buckets), default=0)
    expires = max(
        (bucket.expires_at for bucket in buckets),
        default=now + timedelta(seconds=seconds),
    )
    started = min((bucket.started_at for bucket in buckets), default=now)
    if used >= limit:
        raise RateLimited(math.ceil((expires - now).total_seconds()))
    if consume:
        for subject in sorted(set(subjects)):
            session.execute(
                text(
                    "INSERT INTO app.rate_limit_buckets "
                    "(scope,subject_hash,used,started_at,expires_at) "
                    "VALUES (:scope,:subject,:used,:started,:expires) "
                    "ON CONFLICT (scope,subject_hash) DO UPDATE SET "
                    "used=EXCLUDED.used,started_at=EXCLUDED.started_at,"
                    "expires_at=EXCLUDED.expires_at"
                ),
                dict(
                    scope=scope,
                    subject=subject,
                    used=used + 1,
                    started=started,
                    expires=expires,
                ),
            )


def lock_subjects(session: Session, scope: str, subjects: tuple[str, ...]) -> None:
    if not subjects or len(subjects) > 8:
        raise ValueError("Rate limit configuration unavailable")
    for subject in sorted(set(subjects)):
        # Distinct lock domain from application locks. Hash collisions only
        # serialize unrelated buckets; they cannot grant additional quota.
        lock = int.from_bytes(
            hashlib.sha256(f"rate-limit:{scope}:{subject}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})


def cleanup_buckets(session: Session) -> None:
    """Bounded independent worker tick; no raw IP or journal/policy dependency."""
    session.execute(
        text(
            "DELETE FROM app.rate_limit_buckets WHERE (scope,subject_hash) IN "
            "(SELECT scope,subject_hash FROM app.rate_limit_buckets "
            "WHERE expires_at <= clock_timestamp() ORDER BY expires_at "
            "LIMIT 1000 FOR UPDATE SKIP LOCKED)"
        )
    )
