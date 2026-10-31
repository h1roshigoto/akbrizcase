"""The synthetic harness must fail closed at every external write boundary."""

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.core.db_types import utc_now
from app.modules.orders.deletion_journal import (
    DeletionCommand,
    DurableAcknowledgement,
    JournalConflict,
    JournalUnavailable,
    SyntheticJournal,
    read_witness,
    verify_witness,
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
    assert sink.witness(item, synthetic=True).canonical == item.canonical()
    assert sink.witness(item, synthetic=True).canonical == item.canonical()
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
    assert (
        SyntheticJournal(tmp_path).witness(item, synthetic=True).canonical
        == item.canonical()
    )


def test_private_directory_and_symlink_denial(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    with pytest.raises(JournalUnavailable):
        SyntheticJournal(tmp_path).witness(command(), synthetic=True)
    tmp_path.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        SyntheticJournal(link).witness(command(), synthetic=True)


def test_v1_canonical_bytes_and_completion_based_pruning() -> None:
    item = command()
    original = {
        key: value
        for key, value in item.model_dump(mode="json").items()
        if key
        not in {
            "journal_policy_version",
            "journal_retention_days",
            "basis",
            "completion_required",
        }
    }
    assert (
        item.canonical()
        == json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
    )
    assert DeletionCommand.model_validate_json(item.canonical()) == item
    assert item.earliest_prune_at(None) is None
    later = item.retain_until + timedelta(days=100)
    assert item.earliest_prune_at(later) == later + timedelta(days=14)


def test_v2_manual_basis_wire_and_indefinite_unconfirmed_hold() -> None:
    legacy = command()
    item = DeletionCommand(
        **(
            legacy.model_dump()
            | {
                "format": 2,
                "operation": "order_delete",
                "consent_id": uuid4(),
                "decision_id": uuid4(),
                "policy_version": None,
                "journal_policy_version": "SYNTHETIC-JOURNAL-ONLY",
                "journal_retention_days": 14,
                "basis": "manual_decision",
                "completion_required": True,
            }
        )
    )
    payload = json.loads(item.canonical())
    assert "retain_until" not in payload
    assert payload["minimum_hold_until"] == item.retain_until.isoformat().replace(
        "+00:00", "Z"
    )
    assert payload["completion_required"] is True
    assert payload["retention_days_after_completion"] == 14
    assert payload["policy_version"] is None
    assert DeletionCommand.model_validate_json(item.canonical()) == item
    assert item.earliest_prune_at(None) is None


def test_receipt_without_existing_witness_is_not_proof(tmp_path: Path) -> None:
    from hashlib import sha256

    tmp_path.chmod(0o700)
    sink, item = SyntheticJournal(tmp_path), command()
    receipt = DurableAcknowledgement(item.id, sha256(item.canonical()).hexdigest())
    # A correct digest and UUID do not prove durable existence.
    with pytest.raises(JournalUnavailable, match="missing"):
        verify_witness(sink, item, receipt, synthetic=True)
    receipt = sink.export(item, synthetic=True)
    assert verify_witness(sink, item, receipt, synthetic=True).matches(item)
    with pytest.raises(JournalConflict):
        verify_witness(
            sink, item, DurableAcknowledgement(uuid4(), receipt.sha256), synthetic=True
        )


def test_partial_temporary_and_conflicting_canonical_file(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    sink, item = SyntheticJournal(tmp_path), command()
    (tmp_path / ".interrupted").write_bytes(item.canonical()[:20])
    assert read_witness(sink, item, synthetic=True) is None
    path = tmp_path / (str(item.id) + ".json")
    path.write_bytes(item.canonical()[:20])
    path.chmod(0o600)
    with pytest.raises(JournalConflict):
        read_witness(sink, item, synthetic=True)
    with pytest.raises(JournalConflict):
        sink.witness(item, synthetic=True)


@pytest.mark.parametrize("boundary", [1, 2])
def test_recovery_read_requires_file_and_directory_fsync(
    tmp_path: Path, boundary: int
) -> None:
    import os

    tmp_path.chmod(0o700)
    sink, item = SyntheticJournal(tmp_path), command()
    receipt = sink.export(item, synthetic=True)
    original, calls = os.fsync, 0

    def failing(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == boundary:
            raise OSError("synthetic recovery fsync failure")
        original(fd)

    with patch("os.fsync", side_effect=failing), pytest.raises(OSError):
        verify_witness(sink, item, receipt, synthetic=True)
    assert verify_witness(sink, item, receipt, synthetic=True).matches(item)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "permissions"])
def test_read_rejects_nonprivate_or_nonregular_witness(
    tmp_path: Path, kind: str
) -> None:
    import os

    tmp_path.chmod(0o700)
    sink, item = SyntheticJournal(tmp_path), command()
    path = tmp_path / (str(item.id) + ".json")
    if kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes(item.canonical())
        path.chmod(0o644)
    with pytest.raises((JournalUnavailable, OSError)):
        sink.witness(item, synthetic=True)
