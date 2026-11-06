"""PG16: protected aggregates, real rows, SMTP failure and cleanup/DB signals."""

import json
import logging
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from smtp_double import Sink
from sqlalchemy import select, text, update
from test_email_worker import configuration, create
from test_email_worker import sink as sink
from test_order_creation import creation_client as creation_client
from test_order_creation import creation_db as creation_db
from test_staff import (
    ADMIN_LOGIN,
    ADMIN_PASSWORD,
    STAFF_PASSWORD,
    bootstrap,
)
from test_staff import (
    create as create_staff,
)

from app.core.db import Database, DatabaseSettings
from app.core.operations import authorize, database_report
from app.core.staff import disable_staff
from app.core.staff_models import AuditEvent
from app.modules.chat.models import ChatSession
from app.modules.notify.models import EmailOutbox
from app.modules.notify.smtp import SendFailure
from app.worker.cleanup import cleanup_tick
from app.worker.runner import tick

pytestmark = pytest.mark.integration
CANARY = "private-canary@example.invalid +7 (999) 111-23-45 token-9233 reason-8271"


def test_protected_aggregates_and_failure_signals(
    creation_db: Database,
    creation_client: TestClient,
    staff_database: Database,
    sink: Sink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Both DB fixtures use the sentinel-protected runtime in this isolated stack.
    bootstrap(staff_database)
    create_staff(staff_database, "manager")
    for login, password, expected in [
        (ADMIN_LOGIN, "wrong", "authentication_denied"),
        ("synthetic-new-manager", STAFF_PASSWORD, "forbidden"),
        (ADMIN_LOGIN, ADMIN_PASSWORD, "success"),
    ]:
        with creation_db.unit_of_work() as session:
            assert authorize(session, login, password) == expected
    create(creation_client, creation_db)
    with creation_db.unit_of_work() as session:
        session.execute(
            update(EmailOutbox).values(
                created_at=text("clock_timestamp()-interval '25 hours'")
            )
        )
        session.add(
            ChatSession(owner_token_hash="a" * 64, mode="diagnose", outcome="handoff")
        )
        session.add(
            ChatSession(owner_token_hash="b" * 64, mode="diagnose", outcome="resolved")
        )
    caplog.set_level(logging.INFO)
    with patch("app.worker.cleanup.prepare_batch", side_effect=OSError(CANARY)):
        cleanup_tick(creation_db)

    def fail(*args: object) -> None:
        raise SendFailure(CANARY)

    assert tick(creation_db, configuration(sink), fail)
    with creation_db.unit_of_work() as session:
        report = database_report(session)
        outbox = session.scalars(select(EmailOutbox)).one()
        assert outbox.last_error_code == "SEND_ERROR"
        assert report["order_count"] == 1
        assert report["orders"]["commercial"]["new"] == 1
        assert report["chat_sessions"] == 2
        assert report["chat_technical_outcomes"]["handoff"] == 1
        assert report["chat_technical_outcomes"]["resolved"] == 1
        assert report["answer_usefulness"]["status"] == "unavailable"
        assert report["sales_conversion"]["status"] == "unavailable"
        assert report["postgres_connections"] >= 1
        assert report["outbox"]["counts"]["pending"] == 1
        assert report["outbox"]["oldest_pending_seconds"] >= 90000
        assert {"OUTBOX_PENDING_OVER_24H", "OUTBOX_DELIVERY_ERROR"} <= set(
            report["outbox"]["signals"]
        )
        assert CANARY not in json.dumps(report)
        session.execute(
            update(EmailOutbox).values(
                status="failed", failed_at=text("clock_timestamp()")
            )
        )
    with creation_db.unit_of_work() as session:
        assert "OUTBOX_FAILED" in database_report(session)["outbox"]["signals"]
    assert "SMTP_FAILURE" in caplog.text and "CLEANUP_SCAN_FAILED" in caplog.text
    assert CANARY not in caplog.text
    with creation_db.unit_of_work() as session:
        manager = session.scalar(
            text("SELECT id FROM app.staff_users WHERE login='synthetic-new-manager'")
        )
        assert disable_staff(
            session,
            admin_login=ADMIN_LOGIN,
            admin_password=ADMIN_PASSWORD,
            user_id=manager,
        ).ok
    with creation_db.unit_of_work() as session:
        assert (
            authorize(session, "synthetic-new-manager", STAFF_PASSWORD)
            == "authentication_denied"
        )
        session.execute(text("DELETE FROM app.chat_sessions"))


@pytest.mark.parametrize("details", [{"reason": CANARY}, [], False, None])
def test_orm_audit_insertion_rejects_canary_metadata(
    staff_database: Database, details: object
) -> None:
    with (
        pytest.raises(ValueError, match="^AUDIT_SCHEMA_INVALID$"),
        staff_database.unit_of_work() as session,
    ):
        row = AuditEvent(
            action="staff.login",
            entity_type="staff_user",
            result="success",
            details=details,
        )
        session.add(row)
        session.flush()
    with staff_database.unit_of_work() as session:
        assert session.scalar(text("SELECT count(*) FROM app.audit_events")) == 0


def test_actual_missing_postgres_is_not_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Port 1 in this isolated runner has no PostgreSQL listener; no real data.
    bad = Database(DatabaseSettings("127.0.0.1", 1, "synthetic", "synthetic", CANARY))
    try:
        assert bad.ready() is False
        assert CANARY not in caplog.text
    finally:
        bad.close()
