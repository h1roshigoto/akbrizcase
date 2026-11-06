"""Real SMTP rejection -> failed row -> manager HTTP retry -> local delivery."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from smtp_double import Sink
from test_email_worker import configuration, due, row
from test_email_worker import sink as sink
from test_notification_retry import audits, send
from test_order_reads import business_rows, create, login
from test_order_reads import read_client as read_client

from app.core.db import Database
from app.modules.notify.smtp import send_notification
from app.worker.runner import tick

pytestmark = pytest.mark.integration


def test_real_failure_manager_retry_without_new_order(
    read_client: TestClient, staff_database: Database, sink: Sink
) -> None:
    identifier = create(read_client, staff_database, "support")
    sink.mode = "reject"
    for attempt in range(1, 6):
        assert tick(staff_database, configuration(sink), send_notification)
        assert row(staff_database).attempts == attempt
        if attempt < 5:
            due(staff_database)  # Advance only the synthetic retry clock.
    failed = row(staff_database)
    assert failed.status == "failed" and failed.last_error_code == "SMTP_REJECTED"
    assert not sink.messages
    before = business_rows(staff_database)
    csrf = login(read_client, "manager")
    assert send(read_client, identifier, csrf).status_code == 303
    after = business_rows(staff_database)
    preserved = deepcopy(after)
    preserved["email_outbox"] = before["email_outbox"]
    assert preserved == before
    assert row(staff_database).id == failed.id
    assert row(staff_database).status == "pending"
    assert len(audits(staff_database)) == 1
    assert send(read_client, identifier, csrf).status_code == 409
    assert business_rows(staff_database) == after
    sink.mode = "accept"
    assert tick(staff_database, configuration(sink), send_notification)
    assert row(staff_database).status == "sent" and len(sink.messages) == 1
    assert send(read_client, identifier, csrf).status_code == 409
    assert len(business_rows(staff_database)["orders"]) == 1
    assert len(audits(staff_database)) == 1
