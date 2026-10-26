"""Run only inside the uniquely marked worker-smoke stack; no external SMTP."""

import json
import socket
from email import policy
from email.parser import Parser
from unittest.mock import patch

from app.core.db import Database, DatabaseSettings
from app.modules.notify.models import EmailOutbox
from app.modules.notify.outbox import (
    claim_due,
    database_now,
    finish_failure,
    finish_success,
    recover_expired,
)
from app.modules.notify.smtp import SMTPSettings, send_notification
from app.worker.runner import tick
from sqlalchemy import select, text, update


def control(**command):
    with socket.create_connection(("smtp-test", 1026), 3) as connection:
        connection.sendall(json.dumps(command).encode() + b"\n")
        return json.loads(connection.makefile("rb").readline())


def main():
    settings = SMTPSettings.from_environment()
    assert settings is not None and settings.synthetic and settings.host == "smtp-test"
    database = Database(DatabaseSettings.from_environment())
    try:
        with database.unit_of_work() as session:
            assert (
                session.scalar(
                    text(
                        "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()"
                    )
                )
                == "akbrizcase:synthetic-seed:demo"
            )
            rows = list(session.scalars(select(EmailOutbox).order_by(EmailOutbox.id)))
            assert len(rows) == 2
        messages = control()
        assert len(messages) == 2
        expected = {str(row.id): row for row in rows}
        seen = set()
        for wire in messages:
            mime = Parser(policy=policy.default).parsestr(wire)
            item = next(
                row for key, row in expected.items() if key in str(mime["Message-ID"])
            )
            assert (
                item.status == "sent"
                and item.attempts == 1
                and item.sent_at is not None
            )
            assert item.lease_token is None and item.lease_until is None
            with database.unit_of_work() as session:
                number = session.scalar(
                    text("SELECT number FROM app.orders WHERE id=:id"),
                    {"id": item.order_id},
                )
            assert str(mime["From"]) == settings.sender
            assert str(mime["To"]) == settings.recipient
            assert str(mime["Subject"]) == f"Order #{number}"
            assert (
                mime.get_content()
                == f"Order #{number}\r\n{settings.admin_origin}/admin/orders/{item.order_id}\r\n"
            )
            assert set(mime.keys()) == {
                "From",
                "To",
                "Subject",
                "Message-ID",
                "Content-Type",
                "Content-Transfer-Encoding",
                "MIME-Version",
            }
            seen.add(item.id)
        assert len(seen) == 2
        target = rows[0].id

        def reset():
            with database.unit_of_work() as session:
                session.execute(
                    update(EmailOutbox)
                    .where(EmailOutbox.id == target)
                    .values(
                        status="pending",
                        attempts=0,
                        sent_at=None,
                        lease_token=None,
                        lease_until=None,
                        next_attempt_at=database_now(session),
                        last_error_code=None,
                    )
                )

        def current():
            with database.unit_of_work() as session:
                return session.get(EmailOutbox, target)

        def due():
            with database.unit_of_work() as session:
                session.execute(
                    update(EmailOutbox)
                    .where(EmailOutbox.id == target)
                    .values(next_attempt_at=database_now(session))
                )

        def lease():
            with database.unit_of_work() as session:
                result = claim_due(session)
            assert result is not None
            return result

        def recover():
            with database.unit_of_work() as session:
                session.execute(
                    update(EmailOutbox)
                    .where(EmailOutbox.id == target)
                    .values(lease_until=text("clock_timestamp() - interval '1 second'"))
                )
            with database.unit_of_work() as session:
                assert recover_expired(session) == 1
            due()

        reset()
        control(mode="reject")
        assert tick(database, settings, send_notification)
        assert current().last_error_code == "SMTP_REJECTED"
        assert current().status == "pending" and current().attempts == 1
        due()
        control(mode="timeout")
        with patch("app.modules.notify.smtp.TIMEOUT", 0.2):
            assert tick(database, settings, send_notification)
        assert current().last_error_code == "SMTP_TIMEOUT" and current().attempts == 2
        control(mode="accept")
        due()
        # Crash before SMTP; a stale A cannot overwrite the new live lease B.
        a = lease()
        recover()
        b = lease()
        with database.unit_of_work() as session:
            assert not finish_success(session, a)
            assert not finish_failure(session, a, "SMTP_TIMEOUT")
        assert current().lease_token == b.token and current().attempts == 4
        # Crash after accepted SMTP, before result commit. Duplicate is expected.
        send_notification(settings, b)
        recover()
        assert tick(database, settings, send_notification)
        assert current().status == "sent" and current().attempts == 5
        messages = control()
        assert len(messages) == 4
        assert (
            Parser().parsestr(messages[-1])["Message-ID"]
            == Parser().parsestr(messages[-2])["Message-ID"]
        )
        assert not tick(database, settings, send_notification)
        print(
            "PASS: worker wire/retry/timeout/crash/CAS; stable Message-ID, expected duplicate",
            flush=True,
        )
    finally:
        database.close()


if __name__ == "__main__":
    main()
