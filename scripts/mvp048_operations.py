"""Fixed probe, piped only inside the freshly owned synthetic stack.

No outbox/rate-limit timestamps or states are directly edited. Recovery waits
for production lease and retry deadlines on the real PostgreSQL clock.
"""

import json
import logging
import os
import sys
import tempfile
from datetime import UTC, datetime
from logging.handlers import MemoryHandler
from pathlib import Path

from app.core.db import Database, DatabaseSettings
from app.core.observability import checked_event
from app.core.operations import (
    authorize,
    backup_report,
    database_report,
    disk_report,
    runtime_report,
)
from app.modules.notify.outbox import claim_due
from app.modules.notify.smtp import SMTPSettings, send_notification
from app.worker.runner import tick
from sqlalchemy import text


def main():
    command = json.load(sys.stdin)
    assert command["operation"] in {"report", "outage", "crash", "invariants"}
    database = Database(DatabaseSettings.from_environment())
    try:
        with database.unit_of_work() as session:
            assert (
                session.scalar(
                    text(
                        "SELECT shobj_description(oid,'pg_database') FROM"
                        " pg_database WHERE datname=current_database()"
                    )
                )
                == "akbrizcase:synthetic-seed:demo"
            )
            assert (
                authorize(session, command["login"], command["password"]) == "success"
            )
        if command["operation"] == "report":
            runtime = runtime_report(
                Path(command["runtime_journal"])
                if command.get("runtime_journal")
                else None,
                datetime.now(UTC),
            )
            if "runtime_events" in command:
                events = command["runtime_events"]
                assert type(events) is list and len(events) <= 50000
                raw = "\n".join(json.dumps(checked_event(event)) for event in events)
                assert len(raw.encode()) <= 8 * 1024 * 1024
                with tempfile.TemporaryDirectory(prefix="mvp048-journal-") as temporary:
                    journal = Path(temporary) / "runtime.jsonl"
                    journal.write_text(raw)
                    journal.chmod(0o600)
                    runtime = runtime_report(journal, datetime.now(UTC))
            with database.unit_of_work() as session:
                report = database_report(session)
                extra = session.execute(
                    text(
                        "SELECT count(*) FILTER (WHERE state='active'), "
                        "(SELECT setting::int FROM pg_settings WHERE "
                        "name='max_connections'), (SELECT "
                        "coalesce(sum(attempts),0) FROM app.email_outbox)"
                        " FROM pg_stat_activity WHERE "
                        "datname=current_database()"
                    )
                ).one()
            print(
                json.dumps(
                    {
                        "database": report,
                        "disk": disk_report(Path(os.environ["APP_FILES_DIR"])),
                        "backup": backup_report(
                            Path(command["backup_receipt"])
                            if command.get("backup_receipt")
                            else None,
                            datetime.now(UTC),
                        ),
                        "runtime": runtime,
                        "pg_active": extra[0],
                        "pg_max_connections": extra[1],
                        "outbox_attempts": int(extra[2]),
                        "configuration": {
                            "sqlalchemy_pool": {
                                "status": "configured",
                                "source": "application_factory",
                                "pool_size": database.engine.pool.size(),
                                "max_overflow": database.engine.pool._max_overflow,
                                "timeout_seconds": database.engine.pool.timeout(),
                            }
                        },
                    }
                )
            )
        elif command["operation"] == "invariants":
            with database.unit_of_work() as session:
                counts = [
                    session.scalar(text("SELECT count(*) FROM app." + table))
                    for table in (
                        "orders",
                        "consents",
                        "order_items",
                        "order_status_history",
                        "email_outbox",
                    )
                ]
                assert counts == [command["expected"]] * 5
                bad = session.scalar(
                    text(
                        "SELECT count(*) FROM app.orders o"
                        "\n                    LEFT JOIN app.consents c ON"
                        " c.id=o.consent_id\n                    WHERE "
                        "c.id IS NULL OR c.channel <> 'request_form' OR "
                        "c.revoked_at IS NOT NULL\n                    OR "
                        "o.status <> 'new' OR o.version <> 1 OR o.kind <>"
                        " 'commercial' OR o.source <> 'site'"
                        "\n                    OR o.pii_erased_at IS NOT "
                        "NULL\n                    OR c.text_version <> "
                        ":consent\n                    OR (SELECT count(*)"
                        " FROM app.order_items i WHERE i.order_id=o.id "
                        "AND i.kind_snapshot='product' AND "
                        "i.sku_snapshot='SYNTHETIC035-LIMIT-00' AND "
                        "i.qty=1 AND i.price_mode_snapshot='on_request' "
                        "AND i.unit_price_snapshot IS NULL) <> 1"
                        "\n                    OR (SELECT count(*) FROM "
                        "app.order_status_history h WHERE h.order_id=o.id"
                        " AND h.from_status IS NULL AND h.to_status='new'"
                        " AND h.changed_by IS NULL) <> 1"
                        "\n                    OR (SELECT count(*) FROM "
                        "app.email_outbox e WHERE e.order_id=o.id AND "
                        "e.event_kind='manager_notification') <> 1"
                    ),
                    {"consent": command["consent"]},
                )
                assert bad == 0
            print('{"invariants":"PASS"}')
        else:
            settings = SMTPSettings.from_environment()
            assert settings and settings.synthetic and settings.host == "smtp-test"
            if command["operation"] == "outage":
                logger = logging.getLogger("akbrizcase.runtime")
                capture = MemoryHandler(capacity=10)
                previous_level, previous_propagate = logger.level, logger.propagate
                try:
                    logger.setLevel(logging.INFO)
                    logger.propagate = False
                    logger.addHandler(capture)
                    assert tick(database, settings, send_notification)
                    events = [
                        checked_event(json.loads(record.msg))
                        for record in capture.buffer
                        if not record.args
                    ]
                finally:
                    logger.removeHandler(capture)
                    logger.setLevel(previous_level)
                    logger.propagate = previous_propagate
                    capture.close()
                if (
                    len(events) != 1
                    or events[0]["event_code"] != "SMTP_FAILURE"
                    or events[0].get("errors") != 1
                ):
                    raise ValueError("SMTP_FAILURE_EVIDENCE_UNAVAILABLE")
                with database.unit_of_work() as session:
                    values = session.execute(
                        text(
                            "SELECT count(*), min(extract(epoch FROM "
                            "next_attempt_at-clock_timestamp())) FROM "
                            "app.email_outbox WHERE status='pending' AND "
                            "attempts=1 AND "
                            "last_error_code='SMTP_UNAVAILABLE'"
                        )
                    ).one()
                    assert values[0] == 1 and 285 <= values[1] <= 300
                print(
                    json.dumps(
                        {"smtp_outage": "PASS", "retry_seconds": 300, "events": events}
                    )
                )
            else:
                with database.unit_of_work() as session:
                    claim = claim_due(session)
                assert claim
                send_notification(settings, claim)
                print('{"crash_after_smtp":"PASS","lease_seconds":120}', flush=True)
                # Real process exit after accepted SMTP, before finish_success.
                # This leaves the committed lease for the restarted worker.
                os._exit(0)
    finally:
        database.close()


if __name__ == "__main__":
    main()
