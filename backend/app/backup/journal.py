"""Synthetic full-history discovery with an independently supplied checkpoint.

No production completeness claim: the source must be stopped/fenced, the anchor
obtained from its authority outside the backup, and every writer use this sink.
No pruning API exists. A provider must implement an equivalent authenticated,
non-rollbackable epoch/high-water/fencing protocol before production is possible.
"""

import fcntl
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.backup.format import BackupBlocked, digest, private_read
from app.core.db import Database
from app.modules.kb.files.storage import open_root
from app.modules.orders.deletion_journal import (
    DeletionCommand,
    DurableAcknowledgement,
    SyntheticJournal,
    command_for,
)
from app.modules.orders.deletion_models import DeletionEntry
from app.modules.orders.deletion_operator import authorize
from app.modules.orders.deletion_reconciliation import (
    JournalState,
    postcondition,
    reconcile_command,
)
from app.modules.orders.privacy_service import synthetic_database


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: int = Field(default=1, strict=True, ge=1, le=1)
    epoch: UUID
    count: int = Field(strict=True, ge=0, le=10000)
    inventory_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    fenced_at: AwareDatetime


_INVENTORY = object()


class _Fence:
    active = True


@dataclass(frozen=True, repr=False)
class VerifiedInventory:
    commands: tuple[DeletionCommand, ...]
    sink: "CompleteSyntheticJournal"
    _fence: _Fence
    _verification: object

    def current(self, sink: "CompleteSyntheticJournal") -> bool:
        return (
            self.sink is sink
            and self._verification is _INVENTORY
            and self._fence.active
        )

    def __len__(self) -> int:
        return len(self.commands)


class CompleteSyntheticJournal(SyntheticJournal):
    """Every successful export is indexed before returning its acknowledgement."""

    def __init__(self, directory: Path, epoch: UUID) -> None:
        super().__init__(directory)
        self.epoch = epoch

    @contextmanager
    def _lock(self) -> Iterator[None]:
        directory = open_root(self.directory)
        fd = -1
        try:
            fd = os.open(
                ".inventory-lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise BackupBlocked("JOURNAL_LOCK")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(directory)

    def _commands(self) -> tuple[DeletionCommand, ...]:
        commands = []
        for path in sorted(self.directory.iterdir()):
            if path.name in {".inventory-lock", ".index"}:
                continue
            # Temporary exports or unknown files are evidence of an incomplete
            # attempt; they cannot silently disappear from completeness checks.
            operation_id = UUID(path.stem)
            if path.name != str(operation_id) + ".json":
                raise BackupBlocked("JOURNAL_MEMBER")
            data = self.read(operation_id, synthetic=True)
            if data is None:
                raise BackupBlocked("JOURNAL_MISSING")
            command = DeletionCommand.model_validate_json(data)
            if command.id != operation_id or command.canonical() != data:
                raise BackupBlocked("JOURNAL_CONFLICT")
            commands.append(command)
            if len(commands) > 10000:
                raise BackupBlocked("JOURNAL_LIMIT")
        return tuple(commands)

    def _index(self) -> dict[str, str]:
        try:
            data = json.loads(private_read(self.directory / ".index", 2 * 1024**2))
        except FileNotFoundError:
            return {}
        if data["epoch"] != str(self.epoch):
            raise BackupBlocked("JOURNAL_EPOCH")
        entries = data["entries"]
        if not isinstance(entries, dict):
            raise BackupBlocked("JOURNAL_INDEX")
        return entries

    def export(
        self, command: DeletionCommand, *, synthetic: bool
    ) -> DurableAcknowledgement:
        if not synthetic:
            raise BackupBlocked("INDEPENDENT_JOURNAL_REQUIRED")
        with self._lock():
            receipt = super().export(command, synthetic=True)
            index = self._index()
            if str(command.id) in index and index[str(command.id)] != receipt.sha256:
                raise BackupBlocked("JOURNAL_CONFLICT")
            index[str(command.id)] = receipt.sha256
            from app.backup.storage import atomic_write

            atomic_write(
                self.directory,
                ".index",
                json.dumps(
                    {"epoch": str(self.epoch), "entries": index},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode(),
            )
            return receipt

    @contextmanager
    def fenced(self, expected: Checkpoint) -> Iterator[VerifiedInventory]:
        # Hold the fence until replay AND all final validation finish. A fresh
        # restore cannot use an old anchor to claim knowledge of later deletions.
        with self._lock():
            commands = self._commands()
            index = {str(c.id): digest(c.canonical()) for c in commands}
            actual = digest(
                json.dumps(index, sort_keys=True, separators=(",", ":")).encode()
            )
            if (
                self._index() != index
                or expected.epoch != self.epoch
                or expected.count != len(commands)
                or expected.inventory_sha256 != actual
                or expected.fenced_at > datetime.now(UTC)
                or (datetime.now(UTC) - expected.fenced_at).total_seconds() > 3600
            ):
                raise BackupBlocked("JOURNAL_INCOMPLETE")
            fence = _Fence()
            inventory = VerifiedInventory(
                tuple(sorted(commands, key=lambda c: (c.created_at, str(c.id)))),
                self,
                fence,
                _INVENTORY,
            )
            try:
                yield inventory
            finally:
                fence.active = False

    def checkpoint(self) -> Checkpoint:
        """Source authority operation, before source loss, outside backup archive.

        Synthetic caller is responsible for stopping/fencing the source. Restore
        never generates its own checkpoint from whatever files survived.
        """
        with self._lock():
            commands = self._commands()
            index = {str(c.id): digest(c.canonical()) for c in commands}
            if self._index() != index:
                raise BackupBlocked("JOURNAL_INCOMPLETE")
            return Checkpoint(
                epoch=self.epoch,
                count=len(commands),
                inventory_sha256=digest(
                    json.dumps(index, sort_keys=True, separators=(",", ":")).encode()
                ),
                fenced_at=datetime.now(UTC),
            )


def replay(
    database: Database,
    sink: CompleteSyntheticJournal,
    inventory: VerifiedInventory,
    *,
    login: str,
    password: str,
) -> int:
    if not isinstance(inventory, VerifiedInventory) or not inventory.current(sink):
        raise BackupBlocked("JOURNAL_COMPLETENESS_REQUIRED")
    commands = inventory.commands
    with database.unit_of_work() as session:
        if not synthetic_database(session):
            raise BackupBlocked("SYNTHETIC_RESTORE_REQUIRED")
        external = {c.id: c.canonical() for c in commands}
        # Local APPLIED/PREPARED records cannot compensate for missing external
        # witnesses. In particular, unresolved PREPARED never expires by age.
        for entry in session.scalars(select(DeletionEntry)):
            if external.get(entry.id) != command_for(entry).canonical():
                raise BackupBlocked("JOURNAL_INCOMPLETE")
    for command in commands:
        for _ in range(101):
            result = reconcile_command(
                database,
                command,
                sink,
                execute=True,
                restore_missing=True,
                authorize_manual=lambda s: authorize(s, login, password) == "SUCCESS",
            )
            if result.state == JournalState.APPLIED and result.commit_confirmed:
                break
            if result.state != JournalState.WITNESSED:
                raise BackupBlocked("DELETION_REPLAY_BLOCKED")
        else:
            raise BackupBlocked("DELETION_REPLAY_LIMIT")
    with database.unit_of_work() as session:
        if not all(postcondition(session, c) for c in commands):
            raise BackupBlocked("DELETION_POSTCONDITION")
    return len(commands)
