"""One claim per tick; no checked-out DB connection during SMTP I/O."""

from collections.abc import Callable

from sqlalchemy import text

from app.core.db import Database
from app.modules.notify.outbox import (
    Claim,
    claim_due,
    finish_failure,
    finish_success,
    recover_expired,
)
from app.modules.notify.smtp import SendFailure, SMTPConfigurationError, SMTPSettings


def tick(
    database: Database,
    settings: SMTPSettings | None,
    send: Callable[[SMTPSettings, Claim], None],
) -> bool:
    if settings is None:
        return False
    # Plaintext is confined to explicitly marked, isolated synthetic databases.
    if settings.synthetic:
        with database.unit_of_work() as session:
            marker = session.scalar(
                text(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                    "WHERE datname = current_database()"
                )
            )
            if marker not in {
                "akbrizcase:synthetic-seed:test",
                "akbrizcase:synthetic-seed:demo",
            }:
                raise SMTPConfigurationError()
    with database.unit_of_work() as session:
        recover_expired(session)
    with database.unit_of_work() as session:
        claim = claim_due(session)
    # Claim is usable only after __exit__ committed and closed the Session.
    if claim is None:
        return False
    try:
        send(settings, claim)
    except Exception as error:
        code = error.code if isinstance(error, SendFailure) else "SEND_ERROR"
        with database.unit_of_work() as session:
            finish_failure(session, claim, code)
    else:
        with database.unit_of_work() as session:
            finish_success(session, claim)
    return True
