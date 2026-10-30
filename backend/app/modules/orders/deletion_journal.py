"""Write-ahead deletion commands. Local fsync is a SYNTHETIC harness only.

An exported command is an irrevocable authorization to apply, including after
rollback/restore. It is not a claim of successful execution. Export precedes the
destructive transaction. That transaction atomically records applied_at. There
is deliberately no production sink until independent storage is approved.
"""

import json
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from app.modules.orders.deletion_models import DeletionEntry


class JournalUnavailable(ValueError):
    pass


class DeletionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: int = 1
    id: UUID
    operation: str
    object_id: UUID
    consent_id: UUID | None
    decision_id: UUID | None
    policy_version: str
    created_at: AwareDatetime
    retain_until: AwareDatetime

    @model_validator(mode="after")
    def valid(self) -> "DeletionCommand":
        if self.format != 1 or self.operation not in {
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
        return self

    def canonical(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()


def command_for(entry: DeletionEntry) -> DeletionCommand:
    return DeletionCommand(
        **{
            key: getattr(entry, key)
            for key in DeletionCommand.model_fields
            if key != "format"
        }
    )


class SyntheticJournal:
    """Private directory, atomic install, file+directory fsync, exact retry.

    This cannot attest independence from a backup or host failure. The database
    and APP_ENV must both be synthetic at preparation and application boundaries.
    No configuration flag can authorize this driver for production.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def witness(self, command: DeletionCommand, *, synthetic: bool) -> bytes:
        if not synthetic:
            raise JournalUnavailable("Independent journal storage required")
        descriptor = os.open(
            self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        temporary = "." + uuid4().hex
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise JournalUnavailable("Private synthetic journal required")
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
                existing = os.open(
                    filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                with os.fdopen(existing, "rb") as stream:
                    if stream.read(len(payload) + 1) != payload:
                        raise JournalUnavailable("Journal command conflict") from None
            os.fsync(descriptor)
            return payload
        finally:
            try:
                os.unlink(temporary, dir_fd=descriptor)
            except FileNotFoundError:
                pass
            os.close(descriptor)


def next_midnight(now: datetime) -> datetime:
    from datetime import UTC, timedelta

    return now.astimezone(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
