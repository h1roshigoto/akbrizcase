"""The synthetic harness must fail closed at every external write boundary."""

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.core.db_types import utc_now
from app.modules.orders.deletion_journal import (
    DeletionCommand,
    JournalUnavailable,
    SyntheticJournal,
)


def command() -> DeletionCommand:
    now = utc_now()
    return DeletionCommand(
        id=uuid4(),
        operation="order_pii",
        object_id=uuid4(),
        consent_id=None,
        decision_id=None,
        policy_version="SYNTHETIC-ONLY",
        created_at=now,
        retain_until=now + timedelta(days=14),
    )


def test_atomic_witness_exact_retry_and_production_denial(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    sink, item = SyntheticJournal(tmp_path), command()
    assert sink.witness(item, synthetic=True) == item.canonical()
    assert sink.witness(item, synthetic=True) == item.canonical()
    assert list(tmp_path.iterdir()) == [tmp_path / (str(item.id) + ".json")]
    with pytest.raises(JournalUnavailable):
        sink.witness(item, synthetic=False)
    with pytest.raises(JournalUnavailable):
        sink.witness(item.model_copy(update={"object_id": uuid4()}), synthetic=True)


@pytest.mark.parametrize("boundary", [1, 2])
def test_fsync_failure_never_returns_witness(tmp_path: Path, boundary: int) -> None:
    import os

    tmp_path.chmod(0o700)
    item, original, calls = command(), os.fsync, 0

    def failing(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == boundary:
            raise OSError("synthetic failure")
        original(fd)

    with patch("os.fsync", side_effect=failing), pytest.raises(OSError):
        SyntheticJournal(tmp_path).witness(item, synthetic=True)
    # Before link: no durable intent; after link: exact command is replayable.
    assert SyntheticJournal(tmp_path).witness(item, synthetic=True) == item.canonical()


def test_private_directory_and_symlink_denial(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    with pytest.raises(JournalUnavailable):
        SyntheticJournal(tmp_path).witness(command(), synthetic=True)
    tmp_path.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        SyntheticJournal(link).witness(command(), synthetic=True)
