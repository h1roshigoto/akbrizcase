"""Synthetic browser content, piped only into a freshly guarded disposable API.

No production import/config switch uses this module. The real ClamAV executable
uses the same synthetic signature composition as file acceptance; production
continues to require signed CVD. Publication/review/ACL validators are unchanged.
"""

import io
import json
import os
import secrets
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.core.db import Database, DatabaseSettings
from app.core.staff import authenticate_operator, bootstrap_admin
from app.modules.catalog.write_dto import ItemCreate, ItemState, ModelFields, ModelState
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
from app.modules.kb.models import DocumentSection
from app.modules.kb.tree_lifecycle import run_tree_lifecycle
from app.modules.kb.tree_service import run_tree
from sqlalchemy import select, text


class SyntheticClamAV(ClamAV):
    def _database(self):
        epoch = int((self.database / "test.ndb").stat().st_mtime)
        self.require_fresh(epoch)
        return "mvp035-synthetic-only", epoch


def main(*, operator_login=None, operator_password=None):
    assert os.environ.get("APP_ENV") == "demo"
    database = Database(DatabaseSettings.from_environment())
    login = operator_login or "synthetic035"
    password = operator_password or secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="synthetic035-") as directory:
        root = Path(directory)
        signatures = root / "signatures"
        signatures.mkdir()
        signatures.joinpath("test.ndb").write_text(
            "MVP035.Safe.Test:0:*:" + b"SYNTHETIC035-INFECTED".hex() + "\n"
        )
        pipeline = FilePipeline(
            Path(os.environ["APP_FILES_DIR"]), _scanner=SyntheticClamAV(signatures)
        )
        with database.unit_of_work() as session:
            assert (
                session.scalar(
                    text(
                        "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=current_database()"
                    )
                )
                == "akbrizcase:synthetic-seed:demo"
            )
            assert (
                session.scalar(text("SELECT count(*) FROM app.document_versions")) == 0
            )
            assert session.scalar(text("SELECT count(*) FROM app.chat_sessions")) == 0
            accepted = pipeline.accept(
                io.BytesIO(
                    "Синтетическийквантопылесос: точная цитата для проверки.\n".encode()
                ),
                filename="synthetic.txt",
                mime="text/plain",
            )
            if operator_login is None:
                assert bootstrap_admin(session, login=login, password=password).ok
            actor = authenticate_operator(session, login, password)
            assert actor
            service = CatalogWriteService(session, actor)
            model = service.create_model(
                ModelFields(
                    code="SYNTHETIC035",
                    name="Синтетическая модель чата",
                    description="SYNTHETIC ONLY",
                )
            )
            model = service.change_model_state(
                model.id, ModelState(expected_updated_at=model.updated_at), "publish"
            )
            draft = DraftEditService(session, actor)
            version = draft.import_draft(
                DraftDocument(
                    "SYNTHETIC035", "Синтетический документ чата", "other", "public"
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
                        "intro",
                        SectionContent(
                            "Синтетический раздел",
                            "Синтетическийквантопылесос: точная цитата для проверки.",
                            None,
                            None,
                            True,
                        ),
                    )
                ],
            )
            section = session.scalar(
                select(DocumentSection).where(
                    DocumentSection.document_version_id == version.id
                )
            )
            assert section

            def lifecycle(action, **extra):
                report = run_lifecycle(
                    session,
                    login=login,
                    password=password,
                    operation=action,
                    document_id=version.document_id,
                    pipeline=pipeline,
                    **extra,
                )
                assert report.ok, report.code
                return report

            preview = lifecycle("preview")
            binding = next(
                v
                for v in preview.technical["versions"]
                if v["version_id"] == str(version.id)
            )
            approval = root / "review.json"
            approval.write_text(
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
                        "expert_name": "SYNTHETIC TEST Expert",
                        "expert_reference": "SYNTHETIC:035:REVIEW",
                        "expert_approved_on": datetime.now(UTC).date().isoformat(),
                        "decision": "approved",
                        "content_checked": True,
                        "full_file_public_allowed": True,
                        "public_release_reference": "SYNTHETIC:035:PUBLIC",
                    }
                )
            )
            approval.chmod(0o600)
            lifecycle("review", version_id=version.id, evidence_path=approval)
            lifecycle("publish", version_id=version.id)
            tree_path = root / "tree.yaml"
            tree_path.write_text(
                json.dumps(
                    {
                        "key": "SYNTHETIC035",
                        "version": 1,
                        "model_code": model.code,
                        "title": "Синтетическая проверка оборудования",
                        "start": "question",
                        "nodes": {
                            "question": {
                                "type": "question",
                                "risk": "observation",
                                "text": "Синтетический вопрос диагностики",
                                "source_refs": [str(section.id)],
                                "options": [
                                    {
                                        "id": "a",
                                        "text": "Синтетическое наблюдение",
                                        "next": "result",
                                    },
                                    {
                                        "id": "b",
                                        "text": "Обратиться к специалисту",
                                        "next": "handoff",
                                    },
                                ],
                            },
                            "result": {
                                "type": "result",
                                "risk": "observation",
                                "text": "Синтетический утверждённый результат",
                                "source_refs": [str(section.id)],
                            },
                            "handoff": {
                                "type": "handoff",
                                "risk": "expert_only",
                                "text": "Обратитесь к специалисту.",
                            },
                        },
                    }
                )
            )
            imported = run_tree(
                session,
                login=login,
                password=password,
                operation="import",
                path=tree_path,
                git_commit="a" * 40,
            )
            assert imported.ok and imported.tree_id
            preview = run_tree_lifecycle(
                session,
                login=login,
                password=password,
                operation="preview",
                tree_id=imported.tree_id,
            )
            assert preview.ok
            proof = root / "tree-review.json"
            proof.write_text(
                json.dumps(
                    {
                        "tree_id": str(imported.tree_id),
                        "yaml_checksum": preview.technical["yaml_checksum"],
                        "binding_sha256": preview.technical["binding_sha256"],
                        "expert_name": "SYNTHETIC TEST Expert",
                        "review_reference": "SYNTHETIC:035:REVIEW",
                        "approved_at": datetime.now(UTC).isoformat(),
                        "decision": "approved",
                        "content_checked": True,
                    }
                )
            )
            proof.chmod(0o600)
            published = run_tree_lifecycle(
                session,
                login=login,
                password=password,
                operation="publish",
                tree_id=imported.tree_id,
                path=tree_path,
                evidence_path=proof,
            )
            assert published.ok, published.code
            # Actual published positions for the 50 + 1 browser regression.
            # Use the same staff write service; the marked, fresh DB guard above
            # applies to these fixtures as well as to the document/tree.
            limit_selection = []
            for index in range(50):
                item = service.create_item(
                    ItemCreate(
                        sku=f"SYNTHETIC035-LIMIT-{index:02}",
                        kind="product",
                        name=f"SYNTHETIC LIMIT {index:02}",
                        description="SYNTHETIC ONLY",
                        attributes={},
                        unit="шт.",
                        price_mode="on_request",
                        price=None,
                        currency="RUB",
                        tax_note="SYNTHETIC ONLY",
                        requires_shipping=False,
                    )
                )
                item = service.change_item_state(
                    item.id, ItemState(expected_revision=item.revision), "publish"
                )
                limit_selection.append(
                    {
                        "item_id": str(item.id),
                        "qty": index + 1,
                        "revision": item.revision,
                    }
                )
            metadata = {
                "model_id": str(model.id),
                "document_id": str(version.document_id),
                "version_id": str(version.id),
                "limit_selection": limit_selection,
            }
        print(json.dumps(metadata))
    database.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--existing-operator"]:
        credentials = json.load(sys.stdin)
        main(
            operator_login=credentials["login"],
            operator_password=credentials["password"],
        )
    else:
        assert not sys.argv[1:]
        main()
