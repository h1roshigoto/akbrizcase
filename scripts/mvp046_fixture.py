"""Fixed domain operations for the owned MVP-046 synthetic stack, never HTTP."""

import io
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, text

from app.core.db import Database, DatabaseSettings
from app.core.release import FullReleaseModes, release_modes
from app.core.staff import authenticate_operator
from app.modules.catalog.models import CatalogItem
from app.modules.catalog.write_dto import ItemEdit, ItemFields
from app.modules.catalog.write_service import CatalogWriteService
from app.modules.kb.draft_service import (
    DraftDocument,
    DraftEditService,
    DraftFile,
    KeyedSection,
    SectionContent,
)
from app.modules.kb.files import FilePipeline
from app.modules.kb.files.scanner import ClamAV
from app.modules.kb.lifecycle_service import run_lifecycle
from app.modules.kb.models import Document, DocumentSection, DocumentVersion


class SyntheticClamAV(ClamAV):
    def _database(self):
        epoch = int((self.database / "test.ndb").stat().st_mtime)
        self.require_fresh(epoch)
        return "SYNTHETIC-046-ONLY", epoch


def main():
    payload = json.load(sys.stdin)
    operation = sys.argv[1]
    assert os.environ["APP_ENV"] == "demo" or isinstance(
        release_modes(), FullReleaseModes
    )
    db = Database(DatabaseSettings.from_environment())
    try:
        with tempfile.TemporaryDirectory(prefix="synthetic046-") as temporary:
            root = Path(temporary)
            (root / "test.ndb").write_text(
                "Synthetic046.Test:0:*:" + b"SYNTHETIC046-INFECTED".hex() + "\n"
            )
            pipeline = FilePipeline(
                Path(os.environ["APP_FILES_DIR"]), _scanner=SyntheticClamAV(root)
            )
            with db.unit_of_work() as session:
                assert (
                    session.scalar(
                        text(
                            "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()"
                        )
                    )
                    == "akbrizcase:synthetic-seed:demo"
                )
                actor = authenticate_operator(
                    session, payload["login"], payload["password"]
                )
                assert actor and actor.user.role == "admin"
                if operation == "internal":
                    accepted = pipeline.accept(
                        io.BytesIO(b"SYNTHETIC046-INTERNAL-NEVER-PUBLIC\n"),
                        filename="internal.txt",
                        mime="text/plain",
                    )
                    draft = DraftEditService(session, actor)
                    version = draft.import_draft(
                        DraftDocument(
                            "SYNTHETIC046-INTERNAL",
                            "SYNTHETIC046-PRIVATE-TITLE",
                            "other",
                            "internal",
                        ),
                        1,
                        DraftFile(
                            accepted.file_key,
                            accepted.mime,
                            accepted.size_bytes,
                            accepted.checksum_sha256,
                        ),
                    )
                    draft.reconcile_sections(
                        version.id,
                        [
                            KeyedSection(
                                "fact",
                                SectionContent(
                                    "Private",
                                    "SYNTHETIC046-INTERNAL-NEVER-PUBLIC",
                                    None,
                                    None,
                                    True,
                                ),
                            )
                        ],
                    )
                    preview = run_lifecycle(
                        session,
                        login=payload["login"],
                        password=payload["password"],
                        operation="preview",
                        document_id=version.document_id,
                        pipeline=pipeline,
                    )
                    assert preview.ok
                    binding = next(
                        v
                        for v in preview.technical["versions"]
                        if v["version_id"] == str(version.id)
                    )
                    evidence = root / "review.json"
                    evidence.write_text(
                        json.dumps(
                            {
                                **{
                                    k: binding[k]
                                    for k in (
                                        "version_id",
                                        "checksum_sha256",
                                        "file_binding_sha256",
                                        "sections_sha256",
                                    )
                                },
                                "expert_name": "SYNTHETIC ONLY Expert",
                                "expert_reference": "SYNTHETIC:046:REVIEW",
                                "expert_approved_on": datetime.now(UTC)
                                .date()
                                .isoformat(),
                                "decision": "approved",
                                "content_checked": True,
                                "full_file_public_allowed": False,
                            }
                        )
                    )
                    evidence.chmod(0o600)
                    for action in ("review", "publish"):
                        report = run_lifecycle(
                            session,
                            login=payload["login"],
                            password=payload["password"],
                            operation=action,
                            document_id=version.document_id,
                            version_id=version.id,
                            evidence_path=evidence if action == "review" else None,
                            pipeline=pipeline,
                        )
                        assert report.ok, report.code
                    section = session.scalars(
                        select(DocumentSection).where(
                            DocumentSection.document_version_id == version.id
                        )
                    ).one()
                    result = {
                        "internal_document_id": str(version.document_id),
                        "internal_version_id": str(version.id),
                        "internal_section_id": str(section.id),
                    }
                elif operation in ("revoke_public", "revoke_internal"):
                    code = (
                        "SYNTHETIC035"
                        if operation == "revoke_public"
                        else "SYNTHETIC046-INTERNAL"
                    )
                    document = session.scalars(
                        select(Document).where(Document.code == code)
                    ).one()
                    preview = run_lifecycle(
                        session,
                        login=payload["login"],
                        password=payload["password"],
                        operation="preview",
                        document_id=document.id,
                        pipeline=pipeline,
                    )
                    assert preview.ok
                    report = run_lifecycle(
                        session,
                        login=payload["login"],
                        password=payload["password"],
                        operation="revoke",
                        document_id=document.id,
                        pipeline=pipeline,
                    )
                    assert report.ok, report.code
                    result = True
                elif operation == "chat_cleanup":
                    from datetime import timedelta
                    from app.modules.chat.models import ChatSession
                    from app.modules.orders.cleanup_service import (
                        eligibility,
                        prepare_entry,
                    )
                    from app.modules.orders.deletion_journal import command_for

                    row = session.get(ChatSession, UUID(payload["session_id"]))
                    assert row is not None
                    from app.modules.orders.seed import demo_bundle

                    now = row.created_at + timedelta(
                        days=demo_bundle().retention.chat_storage_days
                    )
                    bundle, eligible, _ = eligibility(session, "chat_delete", row, now)
                    assert eligible
                    command = command_for(
                        prepare_entry(session, "chat_delete", row.id, bundle, now)
                    )
                    result = True
                elif operation == "price":
                    row = session.scalars(
                        select(CatalogItem).where(CatalogItem.sku == "DEMO-P-001")
                    ).one()
                    fields = {k: getattr(row, k) for k in ItemFields.model_fields}
                    fields["price"] = "150.00"
                    CatalogWriteService(session, actor).edit_item(
                        row.id, ItemEdit(**fields, expected_revision=row.revision)
                    )
                    result = True
                elif operation == "consent":
                    from app.core.db_types import utc_now
                    from app.core.privacy import PrivacyBundle
                    from app.modules.orders.models import ConsentText
                    from app.modules.orders.privacy_models import ConsentPublication
                    from app.modules.orders.privacy_service import lock_privacy
                    from app.modules.orders.seed import demo_bundle, DEMO_VERSION

                    lock_privacy(session, shared=False)
                    session.get(ConsentText, DEMO_VERSION).retired_at = utc_now()
                    data = demo_bundle().model_dump(mode="json")
                    data["consent"]["version"] = "SYNTHETIC-DEMO-ONLY-MVP046-v2"
                    bundle = PrivacyBundle.model_validate(data)
                    session.add(
                        ConsentText(
                            **bundle.consent.model_dump(), active_from=utc_now()
                        )
                    )
                    session.flush()
                    session.add(
                        ConsentPublication(
                            consent_version=bundle.consent.version,
                            bundle_checksum=bundle.checksum(),
                            bundle=bundle.model_dump(mode="json"),
                            published_at=utc_now(),
                        )
                    )
                    result = True
                else:
                    raise AssertionError("Unknown fixed operation")
            if operation == "chat_cleanup":
                from app.modules.orders.deletion_journal import SyntheticJournal
                from app.modules.orders.deletion_reconciliation import reconcile_command

                journal = root / "journal"
                journal.mkdir(mode=0o700)
                outcome = reconcile_command(
                    db, command, SyntheticJournal(journal), execute=True, now=now
                )
                assert outcome.commit_confirmed
            # Emission is after the outer UoW COMMIT, never a successful fixture response.
            print(json.dumps(result))
    finally:
        db.close()


if __name__ == "__main__":
    main()
