"""Protected one-shot synthetic backup/restore; production provider is absent."""

import json
import signal
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pyrage  # type: ignore[import-untyped]

from app.backup.format import BackupBlocked, private_read
from app.backup.journal import Checkpoint, CompleteSyntheticJournal
from app.backup.restore import restore
from app.backup.snapshot import PgTools, capture
from app.backup.storage import SyntheticStorage, diagnostics_receipt, publish_verified
from app.core.cli import SafeParser, hidden
from app.core.db import Database, DatabaseSettings
from app.modules.orders.privacy_service import synthetic_database


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--files", type=Path, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--backup-id", type=UUID)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--diagnostics-receipt", type=Path)
    args = parser.parse_args(argv)
    database = None
    previous = {}
    try:

        def cancelled(*_: object) -> None:
            raise BackupBlocked("OPERATION_CANCELLED")

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM):
            previous[sig] = signal.signal(sig, cancelled)
        signal.alarm(600)
        if args.diagnostics_receipt:
            diagnostics_receipt(
                args.diagnostics_receipt, durable_offsite_verified=False
            )
        identity = private_read(args.identity_file, 4096).decode().strip()
        login = hidden("Acting admin login: ")
        password = hidden("Acting admin password: ")
        settings = DatabaseSettings.from_environment(migration=True)
        database = Database(settings)
        store = SyntheticStorage(args.store)
        if args.operation == "backup":
            with database.unit_of_work() as session:
                if not synthetic_database(session):
                    raise BackupBlocked("PRODUCTION_STORAGE_BLOCKED")
            manifest, ciphertext = capture(
                database,
                PgTools(settings),
                args.files,
                login=login,
                password=password,
                application_head=args.head,
                image_digest=args.image_digest,
                recipient=str(pyrage.x25519.Identity.from_str(identity).to_public()),
            )
            publish_verified(store, uuid4(), manifest, ciphertext, identity)
            if args.diagnostics_receipt:
                diagnostics_receipt(
                    args.diagnostics_receipt,
                    durable_offsite_verified=False,
                    snapshot_at=manifest.snapshot_at,
                    verified=True,
                )
            print("SYNTHETIC_BACKUP_VERIFIED; PRODUCTION_OFFSITE_BLOCKED")
        else:
            if not all((args.backup_id, args.journal, args.checkpoint)):
                raise BackupBlocked("JOURNAL_COMPLETENESS_REQUIRED")
            checkpoint = Checkpoint.model_validate_json(
                private_read(args.checkpoint, 4096)
            )
            _, result = restore(
                database,
                PgTools(settings),
                args.files,
                store.read_accepted(args.backup_id),
                identity=identity,
                application_head=args.head,
                image_digest=args.image_digest,
                journal=CompleteSyntheticJournal(args.journal, checkpoint.epoch),
                checkpoint=checkpoint,
                login=login,
                password=password,
            )
            print(json.dumps({"status": "synthetic-ready-for-manual-review", **result}))
        return 0
    except BaseException:
        print("BACKUP_RESTORE_BLOCKED", file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if database is not None:
            database.close()


if __name__ == "__main__":
    sys.exit(main())
