"""MVP-043: real staff CLI/PTY and protected reports over marked PostgreSQL."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from operator_terminal import terminal
from sqlalchemy import select
from test_order_reads import ORIGIN, PASSWORD
from test_order_reads import read_client as read_client

from app.core.db import Database
from app.core.staff_models import StaffSession, StaffUser

pytestmark = pytest.mark.integration
STAFF = "app.core.cli"
REPORT = "app.core.operations"


def credentials(login: str, password: str = PASSWORD) -> list[tuple[bytes, str]]:
    return [(b"Acting admin login:", login), (b"Acting admin password:", password)]


def create_staff(actor: str, login: str, role: str, password: str = PASSWORD) -> int:
    code, _ = terminal(
        STAFF,
        ["staff-create", "--role", role],
        [
            *credentials(actor, password),
            (b"New staff login:", login),
            (b"New individual password", PASSWORD),
            (b"Confirm new password:", PASSWORD),
        ],
    )
    return code


def staff_id(database: Database, login: str) -> str:
    with database.unit_of_work() as session:
        result = session.scalar(select(StaffUser.id).where(StaffUser.login == login))
        assert result is not None
        return str(result)


def disable(database: Database, login: str) -> None:
    code, _ = terminal(
        STAFF,
        ["staff-disable", "--user-id", staff_id(database, login)],
        credentials("read-admin"),
    )
    assert code == 0


def login_as(client: TestClient, login: str) -> None:
    client.cookies.clear()
    csrf = client.get("/admin/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/admin/auth/login",
        json={"login": login, "password": PASSWORD},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert response.status_code == 200


def test_hidden_staff_cli_and_disabled_sessions(
    read_client: TestClient, staff_database: Database
) -> None:
    manager, secondary = "synthetic-043-manager", "synthetic-043-admin"
    assert create_staff("read-admin", manager, "manager") == 0
    assert create_staff("read-admin", secondary, "admin") == 0
    assert create_staff(manager, "synthetic-043-denied-manager", "manager") == 1
    assert create_staff("read-admin", "synthetic-043-wrong", "manager", "wrong") == 1
    for login in (manager, secondary):
        login_as(read_client, login)
        assert read_client.get("/admin/orders").status_code == 200
        disable(staff_database, login)
        assert read_client.get("/admin/orders").status_code == 401
    assert create_staff(secondary, "synthetic-043-disabled", "manager") == 1
    with staff_database.unit_of_work() as session:
        assert not session.scalars(
            select(StaffUser).where(StaffUser.login.like("synthetic-043-denied%"))
        ).all()
        assert (
            session.scalar(
                select(StaffUser.id).where(
                    StaffUser.login.in_(
                        ["synthetic-043-wrong", "synthetic-043-disabled"]
                    )
                )
            )
            is None
        )
        assert all(
            s.revoked_at is not None for s in session.scalars(select(StaffSession))
        )


def test_report_terminal_acl_and_missing_stale_receipts(
    read_client: TestClient, staff_database: Database, tmp_path: Path
) -> None:
    receipt = tmp_path / "receipt.json"
    secondary = "synthetic-043-observer"
    assert create_staff("read-admin", secondary, "admin") == 0
    disable(staff_database, secondary)
    for login in ("read-manager", secondary):
        code, output = terminal(
            REPORT, ["--backup-receipt", str(receipt)], credentials(login)
        )
        assert code == 1 and b"OPERATOR_AUTHORIZATION_DENIED" in output
        assert b'"orders"' not in output and b'"disk"' not in output
    for content, signal in (
        (None, "BACKUP_CONFIRMATION_MISSING_OR_INVALID"),
        (
            {
                "status": "success",
                "checked_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat(),
            },
            "BACKUP_CONFIRMATION_STALE",
        ),
        (
            {
                "format": 2,
                "status": "failure",
                "operation_status": "verified",
                "scope": "synthetic",
                "checked_at": datetime.now(UTC).isoformat(),
                "snapshot_at": datetime.now(UTC).isoformat(),
            },
            "BACKUP_OFFSITE_BLOCKED",
        ),
    ):
        if content is not None:
            # Use one timestamp: snapshot must not be later than verification.
            if "snapshot_at" in content:
                content["snapshot_at"] = str(content["checked_at"])
            receipt.write_text(json.dumps(content))
            receipt.chmod(0o600)
        code, output = terminal(
            REPORT, ["--backup-receipt", str(receipt)], credentials("read-admin")
        )
        assert code == 0 and signal.encode() in output
        assert b'"coverage": "currently_retained_rows"' in output
        assert (
            b'"answer_usefulness": {"reason": "explicit_feedback_source_absent"'
            in output
        )
        assert b"example.invalid" not in output
    # IDs/credentials are never a diagnostic input or a substitute for authority.
    code, output = terminal(REPORT, [], credentials(str(uuid4()), "wrong"))
    assert code == 1 and b"OPERATOR_AUTHORIZATION_DENIED" in output
