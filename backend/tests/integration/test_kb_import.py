"""PostgreSQL 16 + actual ClamAV + sealed PDF worker, synthetic inputs only."""

import hashlib
import io
import json
import os
import pty
import select as fd_select
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.auth import hash_password
from app.core.db import Database
from app.core.staff import disable_staff, lock_staff
from app.core.staff_models import AuditEvent, StaffUser
from app.main import app
from app.modules.kb.draft_service import DraftEditService
from app.modules.kb.files import FilePipeline
from app.modules.kb.files.scanner import ClamAV
from app.modules.kb.import_service import PreparationReport, run_preparation
from app.modules.kb.models import Document, DocumentSection, DocumentVersion

pytestmark = pytest.mark.integration
PASSWORD = "synthetic-kb-only-password"
INFECTED = b"MVP021-SYNTHETIC-INFECTED"


class SyntheticClamAV(ClamAV):
    """Real executable; no production option accepts unsigned test signatures."""

    def _database(self) -> tuple[str, int]:
        epoch = int((self.database / "test.ndb").stat().st_mtime)
        self.require_fresh(epoch)
        return "mvp021-synthetic", epoch


@pytest.fixture
def pipeline(tmp_path: Path) -> FilePipeline:
    database = tmp_path / "signatures"
    database.mkdir()
    (database / "test.ndb").write_text(f"MVP021.Safe.Test:0:*:{INFECTED.hex()}\n")
    root = Path(os.environ["APP_FILES_DIR"]) / ("synthetic-" + uuid4().hex)
    root.mkdir(mode=0o700)
    return FilePipeline(root, _scanner=SyntheticClamAV(database))


@pytest.fixture
def session(databases: tuple[Database, Database]) -> Iterator[Session]:
    with databases[0].engine.connect() as connection, connection.begin() as transaction:
        with Session(connection, expire_on_commit=False) as value:
            yield value
        transaction.rollback()


def admin(session: Session, *, role: str = "admin", active: bool = True) -> StaffUser:
    user = StaffUser(
        login="synthetic-" + uuid4().hex,
        password_hash=hash_password(PASSWORD),
        role=role,
        is_active=active,
    )
    session.add(user)
    session.flush()
    return user


def pdf(*, scanned: bool = False, large: bool = False) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    if not scanned:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        message = b"Synthetic text " * (2000 if large else 1)
        stream.set_data(b"BT /F1 12 Tf 10 100 Td (" + message + b") Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def inputs(
    tmp_path: Path,
    data: bytes = b"Synthetic text\n",
    extension: str = "txt",
    *,
    extract: bool = False,
) -> tuple[Path, Path]:
    identifier = uuid4().hex
    source = tmp_path / (identifier + "." + extension)
    source.write_bytes(data)
    manifest = tmp_path / (identifier + ".json")
    manifest.write_text(
        json.dumps(
            {
                "code": "SYNTHETIC-" + identifier,
                "title": "Synthetic",
                "kind": "other",
                "visibility": "internal",
                "version": 1,
                "checksum_sha256": hashlib.sha256(data).hexdigest(),
                "preparation": "pdf-text" if extract else "manual",
                "sections": []
                if extract
                else [
                    {
                        "section_key": "intro",
                        "title": "Synthetic",
                        "text": "Synthetic text",
                        "page_from": 1 if extension == "pdf" else None,
                        "page_to": 1 if extension == "pdf" else None,
                    }
                ],
            }
        )
    )
    manifest.chmod(0o600)
    return source, manifest


def run(
    session: Session,
    user: StaffUser,
    paths: tuple[Path, Path],
    pipeline: FilePipeline,
    *,
    check: bool = False,
) -> PreparationReport:
    return run_preparation(
        session,
        login=user.login,
        password=PASSWORD,
        source_path=None if check else paths[0],
        manifest_path=paths[1],
        pipeline=pipeline,
        retain_incomplete=True,
    )


def version_for(session: Session, manifest: Path) -> DocumentVersion | None:
    code = json.loads(manifest.read_bytes())["code"]
    return session.scalar(
        select(DocumentVersion).join(Document).where(Document.code == code)
    )


@pytest.mark.parametrize("extension", ["txt", "md", "pdf", "scan"])
def test_real_import_check_noop(
    session: Session, pipeline: FilePipeline, tmp_path: Path, extension: str
) -> None:
    user = admin(session)
    paths = inputs(
        tmp_path,
        pdf(scanned=extension == "scan")
        if extension in {"pdf", "scan"}
        else b"Synthetic text\n",
        "pdf" if extension == "scan" else extension,
        extract=extension == "pdf",
    )
    unaffected = (
        "catalog_items",
        "equipment_models",
        "orders",
        "order_items",
        "consents",
        "consent_texts",
        "email_outbox",
        "order_status_history",
    )
    before = {
        name: session.execute(text(f"SELECT * FROM app.{name} ORDER BY 1")).all()
        for name in unaffected
    }
    assert run(session, user, paths, pipeline).code == "IMPORTED_DRAFT"
    version = version_for(session, paths[1])
    assert version is not None and version.status == "draft"
    assert version.reviewed_by is None
    assert version.approved_at is None and version.published_at is None
    parts = session.scalars(
        select(DocumentSection).where(DocumentSection.document_version_id == version.id)
    ).all()
    assert len(parts) == 1 and not parts[0].assistant_allowed
    if extension == "pdf":
        assert "Synthetic text" in parts[0].text
        assert parts[0].page_from == parts[0].page_to == 1
    assert (
        run(session, user, paths, pipeline, check=True).code
        == "READY_FOR_MANUAL_REVIEW"
    )
    key, part_id = version.file_key, parts[0].id
    assert run(session, user, paths, pipeline).code == "NO_OP"
    assert version.file_key == key and parts[0].id == part_id
    with TestClient(app) as browser:
        for path in (
            f"/api/v1/documents/{version.id}/file",
            "/api/v1/documents/search?q=Synthetic",
            f"/documents/verified/{key}",
        ):
            response = browser.get(path)
            assert response.status_code == 404
            assert "Synthetic text" not in response.text
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.action.like("kb.%"))
    ).all()
    assert len(events) == 3
    assert all(e.actor_id == user.id and e.details == {} for e in events)
    serialized = repr([(e.action, e.result, e.details) for e in events])
    for private in (key, str(paths[0]), str(paths[1]), PASSWORD, "Synthetic text"):
        assert private not in serialized
    assert before == {
        name: session.execute(text(f"SELECT * FROM app.{name} ORDER BY 1")).all()
        for name in unaffected
    }


@pytest.mark.parametrize(
    "role,active,password,unknown,expected",
    [
        ("manager", True, PASSWORD, False, "FORBIDDEN"),
        ("admin", False, PASSWORD, False, "AUTHENTICATION_DENIED"),
        ("admin", True, "wrong", False, "AUTHENTICATION_DENIED"),
        ("admin", True, PASSWORD, True, "AUTHENTICATION_DENIED"),
    ],
)
def test_auth_before_file_access(
    session: Session,
    pipeline: FilePipeline,
    tmp_path: Path,
    role: str,
    active: bool,
    password: str,
    unknown: bool,
    expected: str,
) -> None:
    user = admin(session, role=role, active=active)
    result = run_preparation(
        session,
        login="unknown" if unknown else user.login,
        password=password,
        source_path=tmp_path / "missing",
        manifest_path=tmp_path / "missing",
        pipeline=pipeline,
    )
    assert result.code == expected
    audit = session.scalars(
        select(AuditEvent).where(AuditEvent.action == "kb.import")
    ).one()
    assert audit.actor_id == (user.id if expected == "FORBIDDEN" else None)
    assert not (pipeline.root / "documents").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "checksum",
        "pages",
        "duplicate",
        "invalid",
        "infected",
        "corrupt",
        "missing-av",
        "stale-av",
    ],
)
def test_rejections_create_no_version(
    session: Session, pipeline: FilePipeline, tmp_path: Path, failure: str
) -> None:
    user = admin(session)
    paths = inputs(tmp_path, pdf(), "pdf") if failure == "pages" else inputs(tmp_path)
    manifest = json.loads(paths[1].read_bytes())
    if failure == "checksum":
        manifest["checksum_sha256"] = "a" * 64
    elif failure == "pages":
        manifest["sections"][0]["page_to"] = 2
    elif failure == "duplicate":
        manifest["sections"] *= 2
    elif failure == "invalid":
        manifest["clean"] = True
    elif failure in {"infected", "corrupt"}:
        paths[0].write_bytes(
            INFECTED if failure == "infected" else b"%PDF-1.4\nstartxref\n0\n%%EOF"
        )
        if failure == "corrupt":
            paths[0].rename(paths[0].with_suffix(".pdf"))
            paths = paths[0].with_suffix(".pdf"), paths[1]
        manifest["checksum_sha256"] = hashlib.sha256(paths[0].read_bytes()).hexdigest()
    elif failure == "missing-av":
        pipeline.scanner = ClamAV(tmp_path / "absent")
    elif failure == "stale-av":
        os.utime(pipeline.scanner.database / "test.ndb", (1, 1))
    paths[1].write_text(json.dumps(manifest))
    assert not run(session, user, paths, pipeline).ok
    assert version_for(session, paths[1]) is None


def test_existing_conflicts_and_new_version(
    session: Session, pipeline: FilePipeline, tmp_path: Path
) -> None:
    user = admin(session)
    paths = inputs(tmp_path)
    assert run(session, user, paths, pipeline).ok
    first = version_for(session, paths[1])
    assert first is not None
    original = json.loads(paths[1].read_bytes())
    value = json.loads(paths[1].read_bytes())
    value["sections"][0]["text"] = "Conflicting synthetic"
    paths[1].write_text(json.dumps(value))
    assert run(session, user, paths, pipeline).code == "DRAFT_CONFLICT"
    paths[0].write_bytes(b"Synthetic new file")
    value = original | {
        "checksum_sha256": hashlib.sha256(paths[0].read_bytes()).hexdigest()
    }
    paths[1].write_text(json.dumps(value))
    assert run(session, user, paths, pipeline).code == "CHECKSUM_CONFLICT"
    value["version"] = 2
    value["sections"][0]["assistant_allowed"] = True
    paths[1].write_text(json.dumps(value))
    assert run(session, user, paths, pipeline).code == "IMPORTED_DRAFT"
    versions = session.scalars(
        select(DocumentVersion).where(DocumentVersion.document_id == first.document_id)
    ).all()
    assert len(versions) == 2
    ids = session.scalars(
        select(DocumentSection.id).where(
            DocumentSection.document_version_id.in_([v.id for v in versions])
        )
    ).all()
    assert len(set(ids)) == 2
    assert (
        session.scalar(
            select(DocumentSection.assistant_allowed)
            .join(DocumentVersion)
            .where(
                DocumentVersion.document_id == first.document_id,
                DocumentVersion.version == 2,
            )
        )
        is True
    )


@pytest.mark.parametrize("large", [False, True])
def test_incomplete_pdf_can_be_finished_manually(
    session: Session, pipeline: FilePipeline, tmp_path: Path, large: bool
) -> None:
    user = admin(session)
    paths = inputs(tmp_path, pdf(scanned=not large, large=large), "pdf", extract=True)
    report = run(session, user, paths, pipeline)
    assert report.code == "PREPARATION_INCOMPLETE" and not report.ok
    version = version_for(session, paths[1])
    assert version is not None and version.status == "draft"
    assert (
        session.scalar(
            select(func.count())
            .select_from(DocumentSection)
            .where(DocumentSection.document_version_id == version.id)
        )
        == 0
    )
    manifest = json.loads(paths[1].read_bytes())
    manifest["preparation"] = "manual"
    manifest["sections"] = [
        {
            "section_key": "verified",
            "title": "Synthetic",
            "text": "Manually verified synthetic text",
            "page_from": 1,
            "page_to": 1,
        }
    ]
    paths[1].write_text(json.dumps(manifest))
    assert run(session, user, paths, pipeline).code == "IMPORTED_DRAFT"
    assert run(session, user, paths, pipeline, check=True).ok


@pytest.mark.parametrize("target", ["file", "manifest"])
def test_finalizer_tampering_rolls_back(
    session: Session,
    pipeline: FilePipeline,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    user = admin(session)
    paths = inputs(tmp_path)
    actual = DraftEditService.record_import
    code = json.loads(paths[1].read_bytes())["code"]

    def mutate(self: DraftEditService, *args: object) -> None:
        actual(self, *args)  # type: ignore[arg-type]
        if target == "manifest":
            paths[1].write_text("{}")
        else:
            verified = next((pipeline.root / "documents" / "verified").iterdir())
            verified.chmod(0o600)
            verified.write_bytes(b"Synthetic changed")
            verified.chmod(0o400)

    monkeypatch.setattr(DraftEditService, "record_import", mutate)
    assert run(session, user, paths, pipeline).code in {
        "FILE_CHANGED",
        "MANIFEST_CHANGED",
    }
    assert session.scalar(select(Document.id).where(Document.code == code)) is None
    assert (
        session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.result == "IMPORTED_DRAFT", AuditEvent.actor_id == user.id
            )
        )
        == 0
    )


@pytest.mark.parametrize("state", ["published", "retired", "revoked"])
def test_immutable_revisions(
    session: Session, pipeline: FilePipeline, tmp_path: Path, state: str
) -> None:
    user = admin(session)
    paths = inputs(tmp_path)
    assert run(session, user, paths, pipeline).ok
    version = version_for(session, paths[1])
    assert version is not None
    session.execute(
        update(DocumentVersion)
        .where(DocumentVersion.id == version.id)
        .values(status=state)
    )
    assert not run(session, user, paths, pipeline).ok
    assert not run(session, user, paths, pipeline, check=True).ok
    session.refresh(version)
    assert version.status == state


def test_audit_failure_rolls_back_all_draft_rows(
    session: Session, pipeline: FilePipeline, tmp_path: Path
) -> None:
    user = admin(session)
    paths = inputs(tmp_path)

    def reject_audit(session: Session, context: object, instances: object) -> None:
        if any(isinstance(row, AuditEvent) for row in session.new):
            raise SQLAlchemyError("synthetic audit failure")

    event.listen(session, "before_flush", reject_audit)
    try:
        with pytest.raises(SQLAlchemyError):
            run(session, user, paths, pipeline)
    finally:
        event.remove(session, "before_flush", reject_audit)
    assert version_for(session, paths[1]) is None


@pytest.mark.parametrize(
    "mutation", ["missing", "bytes", "sections", "pages", "assistant"]
)
def test_check_rejects_saved_contradictions(
    session: Session, pipeline: FilePipeline, tmp_path: Path, mutation: str
) -> None:
    user = admin(session)
    paths = inputs(tmp_path)
    assert run(session, user, paths, pipeline).ok
    version = version_for(session, paths[1])
    assert version is not None
    stored = pipeline.root / "documents" / "verified" / version.file_key
    if mutation == "missing":
        stored.unlink()
    elif mutation == "bytes":
        stored.chmod(0o600)
        stored.write_bytes(b"Synthetic changed")
        stored.chmod(0o400)
    else:
        part = session.scalars(
            select(DocumentSection).where(
                DocumentSection.document_version_id == version.id
            )
        ).one()
        if mutation == "sections":
            part.text = "Conflicting synthetic text"
        elif mutation == "pages":
            part.page_from = part.page_to = 1
        else:
            part.assistant_allowed = True
        session.flush()
    assert not run(session, user, paths, pipeline, check=True).ok
    session.refresh(version)
    assert version.status == "draft"


def test_extraction_retention_uses_new_transaction(
    staff_database: Database, pipeline: FilePipeline, tmp_path: Path
) -> None:
    runtime = staff_database
    with runtime.unit_of_work() as setup:
        user = admin(setup)
        login = user.login
    paths = inputs(tmp_path, pdf(scanned=True), "pdf", extract=True)
    for retain in (False, True):
        with runtime.unit_of_work() as work:
            report = run_preparation(
                work,
                login=login,
                password=PASSWORD,
                manifest_path=paths[1],
                source_path=paths[0],
                pipeline=pipeline,
                retain_incomplete=retain,
            )
            assert report.code == "PREPARATION_INCOMPLETE"
            assert report.reason == "MANUAL_TEXT_REQUIRED" and not report.ok
        with runtime.unit_of_work() as verify:
            saved = version_for(verify, paths[1])
            assert (saved is not None) == retain
            if saved is not None:
                assert saved.status == "draft"
                assert (
                    verify.scalar(
                        select(func.count())
                        .select_from(DocumentSection)
                        .where(DocumentSection.document_version_id == saved.id)
                    )
                    == 0
                )


def test_concurrent_import_and_disable(
    staff_database: Database, pipeline: FilePipeline, tmp_path: Path
) -> None:
    runtime = staff_database
    with runtime.unit_of_work() as setup:
        user = admin(setup)
        login = user.login
        manager = admin(setup, role="manager")
        target_id = manager.id
    paths = inputs(tmp_path)

    def import_one() -> str:
        with runtime.unit_of_work() as value:
            return run_preparation(
                value,
                login=login,
                password=PASSWORD,
                source_path=paths[0],
                manifest_path=paths[1],
                pipeline=pipeline,
            ).code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: import_one(), range(2)))
    assert sorted(results) == ["IMPORTED_DRAFT", "NO_OP"]
    with runtime.unit_of_work() as verify:
        version = version_for(verify, paths[1])
        assert version is not None
        assert (
            verify.scalar(
                select(func.count())
                .select_from(DocumentVersion)
                .where(DocumentVersion.document_id == version.document_id)
            )
            == 1
        )
    # A disable that owns the shared staff lock wins before authentication; the
    # importer must wait and then refresh the now-disabled database identity.
    waiting = Event()

    def after_disable() -> str:
        waiting.set()
        return import_one()

    with ThreadPoolExecutor(1) as pool:
        with runtime.unit_of_work() as disabler:
            lock_staff(disabler)
            disabler.execute(
                update(StaffUser)
                .where(StaffUser.login == login)
                .values(is_active=False)
            )
            future = pool.submit(after_disable)
            assert waiting.wait(2)
            for _ in range(100):
                with runtime.engine.connect() as connection:
                    blocked = connection.scalar(
                        text(
                            "SELECT EXISTS (SELECT FROM pg_stat_activity "
                            "WHERE wait_event_type='Lock' "
                            "AND query LIKE 'SELECT pg_advisory_xact_lock%')"
                        )
                    )
                if blocked:
                    break
            assert blocked and not future.done()
        assert future.result(timeout=5) == "AUTHENTICATION_DENIED"
    # Use the existing disable service too; no new bypass/authority path.
    with runtime.unit_of_work() as setup:
        other = admin(setup)
        assert disable_staff(
            setup, admin_login=other.login, admin_password=PASSWORD, user_id=target_id
        ).ok


def test_container_cli_hidden_credentials(
    staff_database: Database, pipeline: FilePipeline, tmp_path: Path
) -> None:
    runtime = staff_database
    with runtime.unit_of_work() as setup:
        user = admin(setup)
        login = user.login
        manager_login = admin(setup, role="manager").login
    paths = inputs(tmp_path)
    # Test-only composition substitutes signature evidence, never the clamscan
    # executable, auth, CLI, parser, storage, or database. Production has no flag.
    script = (
        "import sys,runpy;sys.path.insert(0,'/app/tests/integration');"
        "from test_kb_import import SyntheticClamAV;"
        "from pathlib import Path;"
        "from app.modules.kb.files import FilePipeline;"
        f"FilePipeline.from_environment=lambda:FilePipeline(Path({str(pipeline.root)!r}),_scanner=SyntheticClamAV(Path({str(pipeline.scanner.database)!r})));"
        "runpy.run_module('app.modules.kb.cli',run_name='__main__')"
    )
    for command, expected, acting_login in (
        ("kb-import", "IMPORTED_DRAFT", login),
        ("kb-check", "READY_FOR_MANUAL_REVIEW", login),
        ("kb-import", "FORBIDDEN", manager_login),
    ):
        master, slave = pty.openpty()
        entry = (
            [sys.executable, "-m", "app.modules.kb.cli"]
            if expected == "FORBIDDEN"
            else [sys.executable, "-c", script]
        )
        args = [*entry, command, "--manifest", str(paths[1])]
        if command == "kb-import":
            args += ["--file", str(paths[0])]
        child = subprocess.Popen(
            args, stdin=slave, stdout=slave, stderr=slave, start_new_session=True
        )
        os.close(slave)
        output = b""
        sent_login = sent_password = False
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if fd_select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 8192)
                    except OSError:
                        break
                    output += chunk
                    if b"Acting admin login:" in output and not sent_login:
                        os.write(master, (acting_login + "\n").encode())
                        sent_login = True
                    if b"Acting admin password:" in output and not sent_password:
                        os.write(master, (PASSWORD + "\n").encode())
                        sent_password = True
                if child.poll() is not None:
                    break
            assert child.wait(timeout=2) == (1 if expected == "FORBIDDEN" else 0)
            assert expected.encode() in output
            assert PASSWORD.encode() not in output and b"Synthetic text" not in output
        finally:
            child.kill() if child.poll() is None else None
            child.wait()
            os.close(master)
