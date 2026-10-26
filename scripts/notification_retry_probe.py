"""Fixed operations inside the marked synthetic HTTPS/SMTP stack only."""

import json
import sys
from email import policy
from email.parser import Parser

from app.core.db import Database, DatabaseSettings
from app.modules.notify.models import EmailOutbox
from app.modules.notify.smtp import SMTPSettings, send_notification
from app.worker.runner import tick
from sqlalchemy import select, text, update
from worker_probe import control


def main():
    settings = SMTPSettings.from_environment()
    assert settings and settings.synthetic and settings.host == "smtp-test"
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
            row = session.scalars(select(EmailOutbox)).one()
            target, order_id = row.id, row.order_id
        if sys.argv[1:] == ["fail"]:
            control(mode="reject")
            for attempt in range(1, 6):
                with database.unit_of_work() as session:
                    session.execute(
                        update(EmailOutbox)
                        .where(EmailOutbox.id == target)
                        .values(next_attempt_at=text("clock_timestamp()"))
                    )
                assert tick(database, settings, send_notification)
                with database.unit_of_work() as session:
                    row = session.get(EmailOutbox, target)
                    assert row.attempts == attempt
                    assert row.status == ("failed" if attempt == 5 else "pending")
                    assert row.last_error_code == "SMTP_REJECTED"
            assert not tick(database, settings, send_notification)
            assert control() == []
        elif sys.argv[1:] == ["send"]:
            control(mode="accept")

            def transport(config, claim):
                # Observe the committed lease before real SMTP; no mocked result.
                with database.unit_of_work() as session:
                    row = session.get(EmailOutbox, target)
                    assert row.status == "sending" and row.attempts == 1
                    assert row.lease_token == claim.token and row.lease_until
                    number = session.scalar(
                        text("SELECT number FROM app.orders WHERE id=:id"),
                        {"id": order_id},
                    )
                send_notification(config, claim)
                return number

            assert tick(database, settings, transport)
            with database.unit_of_work() as session:
                row = session.get(EmailOutbox, target)
                assert row.status == "sent" and row.sent_at and row.attempts == 1
                assert row.lease_token is None and row.lease_until is None
                number = session.scalar(
                    text("SELECT number FROM app.orders WHERE id=:id"), {"id": order_id}
                )
            messages = control()
            assert len(messages) == 1
            mime = Parser(policy=policy.default).parsestr(messages[0])
            assert str(mime["Message-ID"]) == f"<outbox.{target}@example.invalid>"
            assert str(mime["Subject"]) == f"Order #{number}"
            assert str(mime["From"]) == settings.sender
            assert str(mime["To"]) == settings.recipient
            assert (
                mime.get_content()
                == f"Order #{number}\r\n{settings.admin_origin}/admin/orders/{order_id}\r\n"
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
            assert not tick(database, settings, send_notification)
        else:
            raise AssertionError("Unknown fixed probe operation")
        print(
            json.dumps(
                {"result": "PASS", "order_id": str(order_id), "outbox_id": str(target)}
            )
        )
    finally:
        database.close()


if __name__ == "__main__":
    main()
