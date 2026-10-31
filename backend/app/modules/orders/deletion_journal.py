"""Write-ahead deletion commands. Local fsync is a SYNTHETIC harness only.

An exported command is an irrevocable authorization to apply, including after
rollback/restore. It is not a claim of successful execution. Export precedes the
destructive transaction. That transaction atomically records applied_at. There
is deliberately no production sink until independent storage is approved.
"""

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.modules.orders.deletion_models import DeletionEntry


class JournalUnavailable(ValueError):
    pass


class JournalConflict(JournalUnavailable):
    pass


class DeletionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: int = Field(default=1, strict=True)
    id: UUID
    operation: str
    object_id: UUID
    consent_id: UUID | None
    decision_id: UUID | None
    policy_version: str | None
    created_at: AwareDatetime
    retain_until: AwareDatetime = Field(
        validation_alias=AliasChoices("retain_until", "minimum_hold_until")
    )
    journal_policy_version: str | None = None
    journal_retention_days: int | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "journal_retention_days", "retention_days_after_completion"
        ),
        strict=True,
        ge=14,
        le=36500,
    )
    basis: Literal["retention", "manual_decision"] | None = None
    completion_required: Literal[True] | None = None

    @model_validator(mode="after")
    def valid(self) -> "DeletionCommand":
        if self.format not in {1, 2} or self.operation not in {
            "order_pii",
            "order_delete",
            "chat_delete",
            "outbox_delete",
            "staff_session_delete",
            "audit_delete",
        }:
            raise ValueError("Invalid deletion command")
        if self.retain_until < self.created_at + timedelta(days=14):
            raise ValueError("Invalid journal retention")
        if (self.operation == "order_delete") != (
            self.consent_id is not None and self.decision_id is not None
        ) or (
            self.operation != "order_delete"
            and (self.consent_id is not None or self.decision_id is not None)
        ):
            raise ValueError("Invalid deletion decision")
        if self.format == 1:
            if self.policy_version is None or any(
                value is not None
                for value in (
                    self.journal_policy_version,
                    self.journal_retention_days,
                    self.basis,
                    self.completion_required,
                )
            ):
                raise ValueError("Invalid legacy deletion command")
        elif (
            self.journal_policy_version is None
            or self.journal_retention_days is None
            or self.completion_required is not True
            or self.retain_until
            != self.created_at + timedelta(days=self.journal_retention_days)
            or self.basis
            != ("manual_decision" if self.operation == "order_delete" else "retention")
            or (self.operation == "order_delete" and self.policy_version is not None)
            or (
                self.operation != "order_delete"
                and self.policy_version != self.journal_policy_version
            )
        ):
            raise ValueError("Invalid deletion basis")
        return self

    @property
    def journal_version(self) -> str:
        value = self.policy_version if self.format == 1 else self.journal_policy_version
        assert value is not None
        return value

    @property
    def retention_days(self) -> int:
        # V1's prepare-time minimum was an exact immutable policy interval.
        return (
            self.journal_retention_days
            if self.journal_retention_days is not None
            else (self.retain_until - self.created_at).days
        )

    def earliest_prune_at(
        self, confirmed_completion_at: datetime | None
    ) -> datetime | None:
        """Contract calculator, NOT an acknowledgement or a pruning capability.

        A production sink must obtain independent durable terminal evidence.
        Neither an expired prepare-time minimum nor an observed failed attempt
        supplies that evidence. Unresolved commands have no expiry.
        """
        if confirmed_completion_at is None:
            return None
        if (
            confirmed_completion_at.utcoffset() is None
            or confirmed_completion_at < self.created_at
        ):
            raise ValueError("Invalid completion evidence")
        return max(
            self.retain_until,
            confirmed_completion_at + timedelta(days=self.retention_days),
        )

    def canonical(self) -> bytes:
        payload = self.model_dump(mode="json")
        if self.format == 1:
            # Preserve already witnessed v1 commands byte-for-byte.
            for key in (
                "journal_policy_version",
                "journal_retention_days",
                "basis",
                "completion_required",
            ):
                payload.pop(key)
        else:
            payload["minimum_hold_until"] = payload.pop("retain_until")
            payload["retention_days_after_completion"] = payload.pop(
                "journal_retention_days"
            )
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def command_for(entry: DeletionEntry) -> DeletionCommand:
    values = {
        key: getattr(entry, key)
        for key in (
            "format",
            "id",
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
    if entry.format == 2:
        values.update(
            journal_retention_days=(entry.retain_until - entry.created_at).days,
            basis="manual_decision"
            if entry.operation == "order_delete"
            else "retention",
            completion_required=True,
        )
    return DeletionCommand.model_validate(values)


@dataclass(frozen=True)
class DurableAcknowledgement:
    """Sink receipt, not proof of execution or a production wire format."""

    operation_id: UUID
    sha256: str


class JournalSink(Protocol):
    """Trusted adapter outside the DB rollback/restore domain.

    Export is create-if-absent by UUID, exact-byte retry, and returns only after
    durable storage. Read must establish durability, not return a buffered write.
    A receipt alone never authorizes mutation. No delete/prune capability exists.
    Production durability, authentication and discovery remain an ADR decision.
    """

    def export(
        self, command: DeletionCommand, *, synthetic: bool
    ) -> DurableAcknowledgement: ...

    def read(self, operation_id: UUID, *, synthetic: bool) -> bytes | None: ...


_VERIFIED = object()


@dataclass(frozen=True)
class VerifiedWitness:
    """Process-local capability from a sink read, never caller-supplied bytes.

    Verify immediately before a fresh mutation UoW. The trusted sink must retain
    witnesses indefinitely until a separately approved terminal/horizon protocol.
    This is an internal trust boundary, not protection against hostile Python code.
    """

    canonical: bytes
    acknowledgement: DurableAcknowledgement
    _verification: object

    def matches(self, command: DeletionCommand) -> bool:
        return self._verification is _VERIFIED and self.canonical == command.canonical()


def verify_witness(
    sink: JournalSink,
    command: DeletionCommand,
    acknowledgement: DurableAcknowledgement,
    *,
    synthetic: bool,
) -> VerifiedWitness:
    payload = command.canonical()
    if acknowledgement != DurableAcknowledgement(
        command.id, sha256(payload).hexdigest()
    ):
        raise JournalConflict("Journal acknowledgement conflict")
    stored = sink.read(command.id, synthetic=synthetic)
    if stored is None:
        raise JournalUnavailable("Durable witness missing")
    if stored != payload:
        raise JournalConflict("Journal command conflict")
    return VerifiedWitness(payload, acknowledgement, _VERIFIED)


def read_witness(
    sink: JournalSink, command: DeletionCommand, *, synthetic: bool
) -> VerifiedWitness | None:
    payload = sink.read(command.id, synthetic=synthetic)
    if payload is None:
        return None
    if payload != command.canonical():
        raise JournalConflict("Journal command conflict")
    return VerifiedWitness(
        payload,
        DurableAcknowledgement(command.id, sha256(payload).hexdigest()),
        _VERIFIED,
    )


class SyntheticJournal:
    """Private directory, atomic install, file+directory fsync, exact retry.

    This cannot attest independence from a backup or host failure. The database
    and APP_ENV must both be synthetic at preparation and application boundaries.
    No configuration flag can authorize this driver for production.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _directory(self, synthetic: bool) -> int:
        if not synthetic:
            raise JournalUnavailable("Independent journal storage required")
        descriptor = os.open(
            self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(descriptor)
            raise JournalUnavailable("Private synthetic journal required")
        return descriptor

    def read(self, operation_id: UUID, *, synthetic: bool) -> bytes | None:
        descriptor = self._directory(synthetic)
        try:
            try:
                file = os.open(
                    str(operation_id) + ".json",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                return None
            with os.fdopen(file, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 16384
                ):
                    raise JournalUnavailable("Invalid synthetic witness")
                payload = stream.read(16385)
                # A crash after link but before directory fsync is not durable
                # evidence until recovery successfully syncs both again.
                os.fsync(stream.fileno())
            os.fsync(descriptor)
            return payload
        finally:
            os.close(descriptor)

    def export(
        self, command: DeletionCommand, *, synthetic: bool
    ) -> DurableAcknowledgement:
        descriptor = self._directory(synthetic)
        temporary = "." + uuid4().hex
        try:
            payload = command.canonical()
            filename = str(command.id) + ".json"
            file = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=descriptor,
            )
            with os.fdopen(file, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(
                    temporary,
                    filename,
                    src_dir_fd=descriptor,
                    dst_dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                if self.read(command.id, synthetic=synthetic) != payload:
                    raise JournalConflict("Journal command conflict") from None
            os.fsync(descriptor)
            return DurableAcknowledgement(command.id, sha256(payload).hexdigest())
        finally:
            try:
                os.unlink(temporary, dir_fd=descriptor)
            except FileNotFoundError:
                pass
            os.close(descriptor)

    def witness(self, command: DeletionCommand, *, synthetic: bool) -> VerifiedWitness:
        return verify_witness(
            self,
            command,
            self.export(command, synthetic=synthetic),
            synthetic=synthetic,
        )


def next_midnight(now: datetime) -> datetime:
    from datetime import UTC, timedelta

    return now.astimezone(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
