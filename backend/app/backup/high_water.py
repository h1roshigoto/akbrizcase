"""Source-fenced, authenticated sequence evidence for the SYNTHETIC drill only.

The authority is created BEFORE backup, outside database/backup rollback, with a
separate 256-bit HMAC key. Only a live PG16 source fence can issue a checkpoint.
A signed file alone is never proof that its source is still fenced. Production
needs an independent durable reservation/fencing authority; none is supplied here.
"""

import hmac
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.backup.format import (
    SHA,
    BackupBlocked,
    Manifest,
    SequenceIdentity,
    canonical,
    digest,
    private_read,
)
from app.backup.storage import atomic_write
from app.core.auth import has_role
from app.core.db import Database
from app.core.staff import authenticate_operator
from app.modules.kb.files.storage import open_root
from app.modules.orders.privacy_service import synthetic_database

MAX_NUMBER = 2**63 - 1
_VERIFIED = object()


def fingerprint(session: Session) -> str:
    if (
        not synthetic_database(session)
        or session.scalar(text("SELECT current_user")) != "akbrizcase_migrator"
        or int(session.scalar(text("SHOW server_version_num"))) // 10000 != 16
    ):
        raise BackupBlocked("SYNTHETIC_SEQUENCE_SOURCE_REQUIRED")
    row = session.execute(
        text(
            "SELECT pg_postmaster_start_time()::text, current_database(), "
            "(SELECT oid::bigint FROM pg_database WHERE datname=current_database()), "
            "inet_server_addr()::text, inet_server_port()"
        )
    ).one()
    return digest(json.dumps(list(row), separators=(",", ":")).encode())


def sequence_state(session: Session) -> tuple[int, bool]:
    if session.execute(
        text(
            "SELECT seqincrement,seqcache,seqcycle,seqmin,seqmax FROM pg_sequence "
            "WHERE seqrelid='app.order_number_seq'::regclass"
        )
    ).one() != (1, 1, False, 1, MAX_NUMBER):
        raise BackupBlocked("SEQUENCE_PARAMETERS_UNSUPPORTED")
    value, called = session.execute(
        text("SELECT last_value,is_called FROM app.order_number_seq")
    ).one()
    return int(value), bool(called)


class AuthorityState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: SequenceIdentity
    high_water: int = Field(strict=True, ge=0, lt=MAX_NUMBER)
    mac: str = Field(pattern=SHA)


class HighWaterCheckpoint(AuthorityState):
    format: int = Field(default=1, strict=True, ge=1, le=1)
    dump_sha256: str = Field(pattern=SHA)
    fence_id: UUID
    issued_at: AwareDatetime


def _mac(value: BaseModel, key: bytes) -> str:
    payload = json.dumps(
        value.model_dump(mode="json", exclude={"mac"}),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.digest(key, payload, "sha256").hex()


@dataclass
class _Lease:
    session: Session
    checkpoint: HighWaterCheckpoint
    backend_pid: int
    transaction_id: int
    active: bool = True


@dataclass(frozen=True, repr=False)
class VerifiedHighWater:
    checkpoint: HighWaterCheckpoint
    _authority: "SyntheticSequenceAuthority"
    _lease: _Lease
    _verification: object

    def check(self, manifest: Manifest) -> int:
        if self._verification is not _VERIFIED or not self._lease.active:
            raise BackupBlocked("SEQUENCE_SOURCE_NOT_FENCED")
        return self._authority.verify(self.checkpoint, manifest, self._lease)


class SyntheticSequenceAuthority:
    def __init__(self, database: Database, root: Path, key_file: Path) -> None:
        self.database, self.root, self.key_file = database, root, key_file

    def _key(self) -> bytes:
        key = private_read(self.key_file, 64)
        if len(key) != 32:
            raise BackupBlocked("SEQUENCE_AUTHORITY_KEY_REQUIRED")
        return key

    def state(self) -> AuthorityState:
        raw = private_read(self.root / "authority.json", 4096)
        state = AuthorityState.model_validate_json(raw)
        if canonical(state) != raw or not hmac.compare_digest(
            state.mac, _mac(state, self._key())
        ):
            raise BackupBlocked("SEQUENCE_AUTHORITY_INVALID")
        return state

    def checkpoint(self) -> HighWaterCheckpoint:
        try:
            raw = private_read(self.root / "checkpoint.json", 4096)
            checkpoint = HighWaterCheckpoint.model_validate_json(raw)
            if (
                canonical(checkpoint) != raw
                or checkpoint.source != self.state().source
                or not hmac.compare_digest(
                    checkpoint.mac, _mac(checkpoint, self._key())
                )
            ):
                raise BackupBlocked("SEQUENCE_CHECKPOINT_INVALID")
            return checkpoint
        except (OSError, ValueError):
            raise BackupBlocked("SEQUENCE_CHECKPOINT_INVALID") from None

    @classmethod
    def create(
        cls, database: Database, root: Path, key_file: Path
    ) -> "SyntheticSequenceAuthority":
        authority = cls(database, root, key_file)
        fd = open_root(root)
        try:
            if os.listdir(fd):
                raise BackupBlocked("SEQUENCE_AUTHORITY_ALREADY_EXISTS")
        finally:
            os.close(fd)
        with database.unit_of_work() as session:
            source = SequenceIdentity(
                id=uuid4(), epoch=uuid4(), fingerprint=fingerprint(session)
            )
            value, called = sequence_state(session)
        state = AuthorityState(
            source=source, high_water=value if called else value - 1, mac="0" * 64
        )
        state = state.model_copy(update={"mac": _mac(state, authority._key())})
        atomic_write(root, "authority.json", canonical(state), replace=False)
        return authority

    @contextmanager
    def fenced(
        self, manifest: Manifest, *, login: str, password: str
    ) -> Iterator[VerifiedHighWater]:
        state = self.state()
        if manifest.sequence_source != state.source:
            raise BackupBlocked("SEQUENCE_SOURCE_MISMATCH")
        lease = None
        try:
            with self.database.unit_of_work() as session:
                session.execute(
                    text("SET LOCAL idle_in_transaction_session_timeout='600s'")
                )
                session.execute(text("SET LOCAL lock_timeout='1s'"))
                actor = authenticate_operator(session, login, password)
                if (
                    actor is None
                    or not has_role(actor.user, "admin")
                    or fingerprint(session) != state.source.fingerprint
                ):
                    raise BackupBlocked("SEQUENCE_SOURCE_MISMATCH")
                # INSERT takes the orders relation lock before default nextval.
                # ALTER SEQUENCE NO CYCLE is idempotent (NO CYCLE is required),
                # takes ShareRowExclusiveLock and also fences direct nextval/setval.
                # No restart/reset, no changed increment/cache or public API.
                session.execute(
                    text("LOCK TABLE app.orders IN ACCESS EXCLUSIVE MODE NOWAIT")
                )
                sequence_state(session)
                session.execute(text("ALTER SEQUENCE app.order_number_seq NO CYCLE"))
                value, called = sequence_state(session)
                maximum = session.scalar(
                    text("SELECT coalesce(max(number),0) FROM app.orders")
                )
                high_water = value if called else value - 1
                if high_water < max(state.high_water, manifest.sequence_floor, maximum):
                    raise BackupBlocked("SEQUENCE_HIGH_WATER_REGRESSED")
                checkpoint = HighWaterCheckpoint(
                    source=state.source,
                    high_water=high_water,
                    dump_sha256=manifest.dump_sha256,
                    fence_id=uuid4(),
                    issued_at=datetime.now(UTC),
                    mac="0" * 64,
                )
                checkpoint = checkpoint.model_copy(
                    update={"mac": _mac(checkpoint, self._key())}
                )
                state = AuthorityState(
                    source=state.source, high_water=high_water, mac="0" * 64
                )
                state = state.model_copy(update={"mac": _mac(state, self._key())})
                # Neither file rolls back with the source DB. A crash never lowers
                # the acknowledged maximum. A surviving file needs a NEW live fence.
                atomic_write(self.root, "authority.json", canonical(state))
                atomic_write(self.root, "checkpoint.json", canonical(checkpoint))
                pid, txid = session.execute(
                    text("SELECT pg_backend_pid(),txid_current()")
                ).one()
                checkpoint = self.checkpoint()
                lease = _Lease(session, checkpoint, pid, txid)
                witness = VerifiedHighWater(checkpoint, self, lease, _VERIFIED)
                witness.check(manifest)
                yield witness
                witness.check(manifest)
        finally:
            if lease is not None:
                lease.active = False

    def verify(
        self, checkpoint: HighWaterCheckpoint, manifest: Manifest, lease: _Lease
    ) -> int:
        if (
            not lease.active
            or checkpoint != self.checkpoint()
            or manifest.sequence_source is None
            or checkpoint.source != manifest.sequence_source
            or checkpoint.source != self.state().source
            or checkpoint.fence_id != lease.checkpoint.fence_id
            or checkpoint.dump_sha256 != manifest.dump_sha256
            or checkpoint.issued_at < manifest.snapshot_at
            or not timedelta(0)
            <= datetime.now(UTC) - checkpoint.issued_at
            <= timedelta(hours=1)
            or checkpoint.high_water != lease.checkpoint.high_water
            or checkpoint.high_water
            < max(manifest.sequence_floor, self.state().high_water)
            or not hmac.compare_digest(checkpoint.mac, _mac(checkpoint, self._key()))
        ):
            raise BackupBlocked("SEQUENCE_CHECKPOINT_INVALID")
        try:
            pid, txid, locked = lease.session.execute(
                text(
                    "SELECT pg_backend_pid(),txid_current(), EXISTS "
                    "(SELECT 1 FROM pg_locks "
                    "WHERE pid=pg_backend_pid() "
                    "AND relation='app.order_number_seq'::regclass "
                    "AND mode='ShareRowExclusiveLock' AND granted)"
                )
            ).one()
            value, called = sequence_state(lease.session)
            if (
                (pid, txid, locked) != (lease.backend_pid, lease.transaction_id, True)
                or fingerprint(lease.session) != checkpoint.source.fingerprint
                or (value if called else value - 1) != checkpoint.high_water
            ):
                raise BackupBlocked("SEQUENCE_SOURCE_NOT_FENCED")
        except SQLAlchemyError:
            raise BackupBlocked("SEQUENCE_SOURCE_NOT_FENCED") from None
        return checkpoint.high_water
