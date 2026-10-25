"""Rotation preserves the established HMAC bytes and fails closed on bad files."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from app.modules.orders.security import OrderSecurity
from app.modules.orders.service import within_replay_window
from app.schemas.orders import OrderCreate


def request() -> OrderCreate:
    return OrderCreate.model_validate(
        {
            "kind": "support",
            "items": [],
            "contact": {"name": "Synthetic", "phone": "+70000000000"},
            "comment": "Synthetic description",
            "source": "site",
            "consent": {"version": "synthetic", "accepted": True},
            "confirmed": True,
        }
    )


def test_real_file_rotation_preserves_legacy_and_new_hashes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    path = tmp_path / "protected-keyring"
    monkeypatch.setenv("ORDER_HMAC_SECRET_FILE", str(path))
    old, active = "synthetic-old-key_" * 3, "synthetic-active-key_" * 3
    path.write_text(old + "\n")
    legacy = OrderSecurity.from_environment()
    assert legacy is not None
    saved = legacy.request_hash(request())
    path.write_text(json.dumps({"active": active, "previous": [old]}))
    rotated = OrderSecurity.from_environment()
    assert rotated is not None
    assert rotated.matches_request(request(), saved)
    assert rotated.request_hash(request()) == OrderSecurity(
        active.encode()
    ).request_hash(request())
    assert not rotated.matches_request(
        request().model_copy(update={"city": "Changed"}), saved
    )
    assert old not in repr(rotated) and active not in repr(rotated)


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "short",
        "x" * 4097,
        " " * 4097 + "valid" * 10,
        "{invalid-json",
        "[]",
        '{"active":"short","previous":[]}',
        json.dumps({"active": "x" * 64}),
        json.dumps({"active": "x" * 64, "previous": None}),
        json.dumps({"active": "x" * 64, "previous": ["short"]}),
        json.dumps({"active": "x" * 64, "previous": ["x" * 64]}),
        json.dumps({"active": "x" * 64, "previous": [str(i) * 64 for i in range(4)]}),
        json.dumps({"active": "x" * 64, "previous": [], "extra": True}),
        json.dumps({"active": 123, "previous": []}),
        json.dumps({"active": "x" * 64, "previous": [None]}),
        json.dumps({"active": "x" * 64, "previous": [" y" * 32]}),
        '{"active":"' + "x" * 64 + '","active":"' + "y" * 64 + '","previous":[]}',
    ],
)
def test_bad_keyring_disables_order_security(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    contents: str,
) -> None:
    path = tmp_path / "protected-keyring"
    path.write_text(contents)
    monkeypatch.setenv("ORDER_HMAC_SECRET_FILE", str(path))
    assert OrderSecurity.from_environment() is None


def test_missing_and_unreadable_secret_fail_closed(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("ORDER_HMAC_SECRET_FILE", raising=False)
    assert OrderSecurity.from_environment() is None
    monkeypatch.setenv("ORDER_HMAC_SECRET_FILE", str(tmp_path / "absent"))
    assert OrderSecurity.from_environment() is None
    monkeypatch.setenv("ORDER_HMAC_SECRET_FILE", str(tmp_path))
    assert OrderSecurity.from_environment() is None


@pytest.mark.parametrize("offset,eligible", [(-1, True), (0, True), (1, False)])
def test_exact_twenty_four_hour_boundary(offset: int, eligible: bool) -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    now = created + timedelta(hours=24, microseconds=offset)
    assert within_replay_window(created, now) is eligible
