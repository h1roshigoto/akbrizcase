"""MVP-023: real PostgreSQL/ClamAV, operator evidence and HTTP lifecycle.

All approval names/references explicitly describe synthetic test fixtures.
Rollback fixtures preserve existing data; concurrency retains immutable records
only in the marked disposable integration volume.
"""

import io
import json
import os
import pty
import secrets
import select as fd_select
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from uuid import UUID, uuid4

import pytest
import uvicorn
from httpx2 import Client
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session
from test_kb_import import (
    PASSWORD,
    admin,
    inputs,
    pdf,
    version_for,
)
from test_kb_import import pipeline as pipeline
from test_kb_import import session as session

from app.core.auth import AuthenticatedStaff
from app.core.csrf import SESSION_COOKIE
from app.core.db import Database
from app.core.staff import authenticate_operator, disable_staff
from app.core.staff_models import AuditEvent, StaffSession, StaffUser
from app.core.staff_sessions import token_hash
from app.main import app
from app.modules.catalog.models import EquipmentModel
from app.modules.kb.access import PUBLIC_VIEWER
from app.modules.kb.draft_service import (
    DraftDocument,
    DraftEditService,
    DraftFile,
    KeyedSection,
    SectionContent,
)
from app.modules.kb.files import FilePipeline, FileRejected
from app.modules.kb.files.scanner import ScanStamp
from app.modules.kb.lifecycle_service import (
    LifecycleReport,
    Operation,
    run_lifecycle,
)
from app.modules.kb.models import DocumentSection, DocumentVersion
from app.modules.kb.read_service import KnowledgeReadService, SourceNotFound, file_url
from app.modules.kb.router import file_pipeline

pytestmark = pytest.mark.integration


def operation(
    session: Session,
    user: str,
    pipeline: FilePipeline,
    action: Operation,
    document: UUID,
    version: UUID | None = None,
    evidence: Path | None = None,
    visibility: str | None = None,
) -> LifecycleReport:
    return run_lifecycle(
        session,
        login=user,
        password=PASSWORD,
        operation=action,
        document_id=document,
        version_id=version,
        evidence_path=evidence,
        visibility=visibility,
        pipeline=pipeline,
    )


def draft(
    session: Session,
    pipeline: FilePipeline,
    tmp_path: Path,
    *,
    visibility: str = "internal",
    extension: str = "txt",
) -> tuple[str, UUID, UUID]:
    # Trusted synthetic fixture setup through the draft service. The actual
    # import/check/hidden-input path is separately exercised by the PTY test;
    # every lifecycle review and publish below still performs fresh real AV.
    # Prepare bytes before the first DB write: fixture setup does not inherit
    # the authenticated operator's bounded idle-timeout adjustment.
    accepted = pipeline.accept(
        io.BytesIO(pdf() if extension == "pdf" else b"Synthetic text\n"),
        filename="synthetic." + extension,
        mime="application/pdf" if extension == "pdf" else "text/plain",
    )
    user = admin(session)
    service = DraftEditService(session, AuthenticatedStaff(user))
    version = service.import_draft(
        DraftDocument("SYNTHETIC-023-" + uuid4().hex, "Synthetic", "other", visibility),
        1,
        DraftFile(
            accepted.file_key,
            accepted.mime,
            accepted.size_bytes,
            accepted.checksum_sha256,
        ),
    )
    service.reconcile_sections(
        version.id,
        [
            KeyedSection(
                "intro",
                SectionContent(
                    "Synthetic",
                    "Синтетический насос",
                    1 if extension == "pdf" else None,
                    1 if extension == "pdf" else None,
                    True,
                ),
            )
        ],
    )
    assert version.reviewed_by is None and version.approved_at is None
    return user.login, version.document_id, version.id


def approval(
    session: Session,
    pipeline: FilePipeline,
    tmp_path: Path,
    login: str,
    document: UUID,
    version: UUID,
    *,
    public: bool = False,
) -> Path:
    report = operation(session, login, pipeline, "preview", document)
    assert report.ok
    rows = report.technical["versions"]
    assert isinstance(rows, list)
    row = next(r for r in rows if r["version_id"] == str(version))
    values = {
        key: row[key]
        for key in (
            "version_id",
            "checksum_sha256",
            "file_binding_sha256",
            "sections_sha256",
        )
    }
    path = tmp_path / (uuid4().hex + ".json")
    path.write_text(
        json.dumps(
            {
                **values,
                "expert_name": "SYNTHETIC TEST Expert",
                "expert_reference": "SYNTHETIC:APPROVAL:1",
                "expert_approved_on": datetime.now(UTC).date().isoformat(),
                "decision": "approved",
                "content_checked": True,
                "full_file_public_allowed": public,
                **(
                    {"public_release_reference": "SYNTHETIC:FULL-FILE:1"}
                    if public
                    else {}
                ),
            }
        )
    )
    path.chmod(0o600)
    return path


def review_publish(
    session: Session,
    pipeline: FilePipeline,
    tmp_path: Path,
    login: str,
    document: UUID,
    version: UUID,
    *,
    public: bool = False,
) -> Path:
    path = approval(
        session, pipeline, tmp_path, login, document, version, public=public
    )
    assert (
        operation(session, login, pipeline, "review", document, version, path).code
        == "REVIEWED"
    )
    assert (
        operation(session, login, pipeline, "publish", document, version).code
        == "PUBLISHED"
    )
    return path


def second_draft(session: Session, document: UUID, original: UUID) -> UUID:
    old = session.get(DocumentVersion, original)
    assert old is not None
    version = DocumentVersion(
        document_id=document,
        version=old.version + 1,
        file_key=old.file_key,
        mime=old.mime,
        size_bytes=old.size_bytes,
        checksum_sha256=old.checksum_sha256,
    )
    session.add(version)
    session.flush()
    session.add(
        DocumentSection(
            document_version_id=version.id,
            section_key="intro",
            title="Синтетическая новая редакция",
            text="Синтетический насос",
            assistant_allowed=True,
        )
    )
    session.flush()
    return version.id


def test_lifecycle_review_is_explicit_and_publish_retires(
    session: Session, pipeline: FilePipeline, tmp_path: Path
) -> None:
    login, document, first = draft(session, pipeline, tmp_path, visibility="public")
    assert (
        operation(session, login, pipeline, "publish", document, first).code
        == "REVIEW_REQUIRED"
    )
    proof = review_publish(
        session, pipeline, tmp_path, login, document, first, public=True
    )
    old = session.get(DocumentVersion, first)
    assert (
        old is not None and old.reviewed_by is not None and old.approved_at is not None
    )
    timestamp = old.published_at
    assert (
        operation(session, login, pipeline, "publish", document, first).code == "NO_OP"
    )
    assert old.published_at == timestamp
    second = second_draft(session, document, first)
    review_publish(session, pipeline, tmp_path, login, document, second, public=True)
    session.refresh(old)
    assert old.status == "retired" and old.published_at == timestamp
    assert (
        session.scalar(
            select(func.count())
            .select_from(DocumentVersion)
            .where(
                DocumentVersion.document_id == document,
                DocumentVersion.status == "published",
            )
        )
        == 1
    )
    assert (
        operation(session, login, pipeline, "retire", document, second).code
        == "RETIRED"
    )
    assert (
        operation(session, login, pipeline, "retire", document, second).code == "NO_OP"
    )
    assert (
        operation(session, login, pipeline, "revoke", document, first).code == "REVOKED"
    )
    assert (
        operation(session, login, pipeline, "publish", document, first).code
        == "DRAFT_REQUIRED"
    )
    assert operation(session, login, pipeline, "revoke", document).code == "REVOKED"
    assert operation(session, login, pipeline, "revoke", document).code == "NO_OP"
    assert (
        operation(
            session, login, pipeline, "visibility", document, visibility="public"
        ).code
        == "DOCUMENT_REVOKED"
    )
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.entity_id.in_([document, first, second]))
    ).all()
    assert all(e.actor_id == old.reviewed_by for e in events)
    serialized = json.dumps([e.details for e in events])
    for forbidden in (old.file_key, str(proof), "Синтетический насос", PASSWORD):
        assert forbidden not in serialized
    assert "SYNTHETIC:APPROVAL:1" in serialized


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "Changed"),
        ("text", "Different"),
        ("assistant_allowed", False),
        ("page_from", 1),
        ("page_to", 2),
    ],
)
def test_lifecycle_stale_sections_block_publish(
    session: Session, pipeline: FilePipeline, tmp_path: Path, field: str, value: object
) -> None:
    login, document, version = draft(session, pipeline, tmp_path)
    proof = approval(session, pipeline, tmp_path, login, document, version)
    assert operation(session, login, pipeline, "review", document, version, proof).ok
    part = session.scalar(
        select(DocumentSection).where(DocumentSection.document_version_id == version)
    )
    assert part is not None
    if field in {"page_from", "page_to"}:
        part.page_from = part.page_to = 1
    setattr(part, field, value)
    session.flush()
    assert (
        operation(session, login, pipeline, "publish", document, version).code
        == "REVIEW_STALE"
    )
    assert session.get(DocumentVersion, version).status == "draft"  # type: ignore[union-attr]


def test_lifecycle_review_again_after_edit(
    session: Session, pipeline: FilePipeline, tmp_path: Path
) -> None:
    login, document, version = draft(session, pipeline, tmp_path)
    proof = approval(session, pipeline, tmp_path, login, document, version)
    assert (
        operation(session, login, pipeline, "review", document, version, proof).code
        == "REVIEWED"
    )
    assert (
        operation(session, login, pipeline, "review", document, version, proof).code
        == "NO_OP"
    )
    part = session.scalar(
        select(DocumentSection).where(DocumentSection.document_version_id == version)
    )
    assert part is not None
    actor = authenticate_operator(session, login, PASSWORD)
    assert actor is not None
    DraftEditService(session, actor).edit_section(
        part.id, SectionContent("Changed", "Синтетический насос", None, None, True)
    )
    assert (
        operation(session, login, pipeline, "publish", document, version).code
        == "REVIEW_STALE"
    )
    proof = approval(session, pipeline, tmp_path, login, document, version)
    assert (
        operation(session, login, pipeline, "review", document, version, proof).code
        == "REVIEWED"
    )
    assert (
        operation(session, login, pipeline, "publish", document, version).code
        == "PUBLISHED"
    )


@pytest.mark.parametrize(
    "role,active,expected",
    [("manager", True, "FORBIDDEN"), ("admin", False, "AUTHENTICATION_DENIED")],
)
@pytest.mark.parametrize(
    "action", ["review", "publish", "retire", "revoke", "visibility", "preview"]
)
def test_lifecycle_admin_only(
    session: Session,
    pipeline: FilePipeline,
    tmp_path: Path,
    role: str,
    active: bool,
    expected: str,
    action: Operation,
) -> None:
    # Deny before even looking up caller-supplied IDs or opening evidence/files.
    document, version = uuid4(), uuid4()
    user = admin(session, role=role, active=active)
    assert (
        operation(
            session,
            user.login,
            pipeline,
            action,
            document,
            version,
            tmp_path / "never-opened",
        ).code
        == expected
    )


def test_lifecycle_preview_actual_dependencies(
    session: Session, pipeline: FilePipeline, tmp_path: Path
) -> None:
    login, document, version = draft(session, pipeline, tmp_path)
    model = EquipmentModel(
        code="SYNTHETIC-" + uuid4().hex,
        name="Private title",
        description="Private text",
        manual_document_id=document,
    )
    session.add(model)
    session.flush()
    report = operation(session, login, pipeline, "preview", document)
    assert report.technical["dependencies"] == [
        {
            "kind": "equipment_models.manual_document_id",
            "count": 1,
            "ids": [str(model.id)],
        },
        {
            "kind": "catalog_item_models.evidence_section_id",
            "count": 0,
            "ids": [],
            "links": [],
            "link_count": 0,
        },
    ]
    assert "Private" not in json.dumps(report.technical)
    assert operation(session, login, pipeline, "revoke", document).ok
    assert model.manual_document_id == document
    assert (
        operation(session, login, pipeline, "publish", uuid4(), version).code
        == "NOT_FOUND"
    )


@pytest.mark.parametrize(
    "mode",
    [
        "missing",
        "stale",
        "unsigned",
        "infected",
        "timeout",
        "finalizer",
        "metadata",
        "replacement",
    ],
)
def test_lifecycle_files_fail_closed(
    session: Session,
    pipeline: FilePipeline,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    login, document, version_id = draft(session, pipeline, tmp_path)
    proof = approval(session, pipeline, tmp_path, login, document, version_id)
    assert operation(session, login, pipeline, "review", document, version_id, proof).ok
    version = session.get(DocumentVersion, version_id)
    assert version is not None
    if mode == "missing":
        pipeline.scanner.database.rename(tmp_path / "removed-signatures")
    elif mode == "stale":
        signature = pipeline.scanner.database / "test.ndb"
        os.utime(signature, (1, 1))
    elif mode == "unsigned":
        from app.modules.kb.files.scanner import ClamAV

        pipeline.scanner = ClamAV(pipeline.scanner.database)
    elif mode == "infected":
        (pipeline.scanner.database / "test.ndb").write_text(
            "MVP023.Safe.Test:0:*:" + b"Synthetic".hex() + "\n"
        )
    elif mode == "timeout":

        def fail(descriptor: int) -> None:
            raise FileRejected("PROCESS_TIMEOUT")

        monkeypatch.setattr(pipeline.scanner, "scan", fail)
    elif mode in {"finalizer", "replacement"}:
        original = pipeline.prepare

        @contextmanager
        def altered(key: str) -> Iterator[tuple[object, int]]:
            with original(key) as result:
                yield result
                if mode == "finalizer":
                    raise FileRejected("FILE_CHANGED")
                path = pipeline.root / "documents" / "verified" / key
                path.chmod(0o600)
                path.write_bytes(b"Replacement bytes\n")
                path.chmod(0o400)

        monkeypatch.setattr(pipeline, "prepare", altered)
    else:
        version.size_bytes += 1
        session.flush()
    report = operation(session, login, pipeline, "publish", document, version_id)
    assert not report.ok
    session.refresh(version)
    assert version.status == "draft" and version.published_at is None
    assert (
        session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "kb.publish",
                AuditEvent.entity_id == version_id,
                AuditEvent.result == "success",
            )
        )
        == 0
    )


def test_lifecycle_empty_mismatched_public_and_pages(
    session: Session, pipeline: FilePipeline, tmp_path: Path
) -> None:
    login, document, version_id = draft(
        session, pipeline, tmp_path, visibility="public", extension="pdf"
    )
    proof = approval(session, pipeline, tmp_path, login, document, version_id)
    assert operation(session, login, pipeline, "review", document, version_id, proof).ok
    assert (
        operation(session, login, pipeline, "publish", document, version_id).code
        == "PUBLIC_APPROVAL_REQUIRED"
    )
    part = session.scalar(
        select(DocumentSection).where(DocumentSection.document_version_id == version_id)
    )
    assert part is not None
    part.page_to = 2
    session.flush()
    proof = approval(
        session, pipeline, tmp_path, login, document, version_id, public=True
    )
    assert (
        operation(session, login, pipeline, "review", document, version_id, proof).code
        == "PAGES_INVALID"
    )
    session.delete(part)
    session.flush()
    proof = approval(session, pipeline, tmp_path, login, document, version_id)
    assert (
        operation(session, login, pipeline, "review", document, version_id, proof).code
        == "SECTIONS_REQUIRED"
    )
    assert (
        operation(session, login, pipeline, "retire", document, version_id).code
        == "PUBLISHED_REQUIRED"
    )


def test_lifecycle_audit_rollback_preserves_old_publication(
    session: Session, pipeline: FilePipeline, tmp_path: Path
) -> None:
    login, document, first = draft(session, pipeline, tmp_path)
    review_publish(session, pipeline, tmp_path, login, document, first)
    second = second_draft(session, document, first)
    proof = approval(session, pipeline, tmp_path, login, document, second)
    assert operation(session, login, pipeline, "review", document, second, proof).ok

    def fail(mapper: object, connection: object, target: AuditEvent) -> None:
        if target.action == "kb.publish":
            raise SQLAlchemyError("synthetic audit failure")

    event.listen(AuditEvent, "before_insert", fail)
    try:
        with pytest.raises(SQLAlchemyError):
            operation(session, login, pipeline, "publish", document, second)
    finally:
        event.remove(AuditEvent, "before_insert", fail)
    assert session.get(DocumentVersion, first).status == "published"  # type: ignore[union-attr]
    assert session.get(DocumentVersion, second).status == "draft"  # type: ignore[union-attr]


@contextmanager
def live_http(database: Database, pipeline: FilePipeline) -> Iterator[Client]:
    """Actual localhost TCP HTTP, not an in-process ASGI transport."""
    app.state.database = database
    app.dependency_overrides[file_pipeline] = lambda: pipeline
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, lifespan="off")
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with Client(
            base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
            trust_env=False,
            timeout=15,
        ) as browser:
            yield browser
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        app.dependency_overrides.clear()
        assert not thread.is_alive()


def test_lifecycle_real_tcp_http_visibility_retire_revoke(
    database: Database, pipeline: FilePipeline, tmp_path: Path
) -> None:
    with database.unit_of_work() as session:
        login, document, first = draft(session, pipeline, tmp_path, visibility="public")
        session.execute(
            text(
                "UPDATE app.document_sections SET text='MVP023HTTP насос' "
                "WHERE document_version_id=:id"
            ),
            {"id": first},
        )
        review_publish(session, pipeline, tmp_path, login, document, first, public=True)
        actor = authenticate_operator(session, login, PASSWORD)
        assert actor is not None
        token = secrets.token_hex(32)
        session.add(
            StaffSession(
                user_id=actor.user.id,
                token_hash=token_hash(token),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        part = session.scalar(
            select(DocumentSection.id).where(
                DocumentSection.document_version_id == first
            )
        )
        assert part is not None
    url = file_url(document, first)
    with live_http(database, pipeline) as browser:
        assert browser.get(url).status_code == 200
        result = browser.get(
            "/api/v1/knowledge/search", params={"q": "MVP023HTTP", "limit": 5}
        ).json()
        assert any(i["source"]["document_id"] == str(document) for i in result["items"])
        with database.unit_of_work() as session:
            second = second_draft(session, document, first)
            session.execute(
                text(
                    "UPDATE app.document_sections SET text='MVP023HTTP насос' "
                    "WHERE document_version_id=:id"
                ),
                {"id": second},
            )
            review_publish(
                session, pipeline, tmp_path, login, document, second, public=True
            )
        assert browser.get(url).headers["x-document-version-status"] == "retired"
        current = browser.get("/api/v1/documents").json()
        assert next(i for i in current["items"] if i["id"] == str(document))[
            "document_version_id"
        ] == str(second)
        with database.unit_of_work() as session:
            assert operation(
                session, login, pipeline, "visibility", document, visibility="internal"
            ).ok
        assert browser.get(url).status_code == 404
        assert not any(
            i["id"] == str(document)
            for i in browser.get("/api/v1/documents").json()["items"]
        )
        browser.cookies.set(SESSION_COOKIE, token)
        assert browser.get(url).status_code == 200
        browser.cookies.clear()
        with database.unit_of_work() as session:
            assert (
                operation(
                    session,
                    login,
                    pipeline,
                    "visibility",
                    document,
                    visibility="public",
                ).code
                == "PUBLIC_APPROVAL_REQUIRED"
            )
            preview = operation(
                session, login, pipeline, "preview", document
            ).technical["versions"]
            assert isinstance(preview, list)
            bindings = [
                {
                    k: row[k]
                    for k in (
                        "version_id",
                        "checksum_sha256",
                        "file_binding_sha256",
                        "sections_sha256",
                    )
                }
                for row in preview
            ]
            release = tmp_path / "public-release.json"
            release.write_text(
                json.dumps(
                    {
                        "document_id": str(document),
                        "release_reference": "SYNTHETIC:RELEASE:1",
                        "full_files_public_allowed": True,
                        "versions": bindings[1:],
                    }
                )
            )
            release.chmod(0o600)
            assert (
                operation(
                    session,
                    login,
                    pipeline,
                    "visibility",
                    document,
                    evidence=release,
                    visibility="public",
                ).code
                == "PUBLIC_APPROVAL_STALE"
            )
            value = json.loads(release.read_text())
            value["versions"] = bindings
            release.write_text(json.dumps(value))
            assert operation(
                session,
                login,
                pipeline,
                "visibility",
                document,
                evidence=release,
                visibility="public",
            ).ok
        assert browser.get(url).status_code == 200
        with database.unit_of_work() as session:
            assert operation(session, login, pipeline, "revoke", document, second).ok
        assert browser.get(file_url(document, second)).status_code == 404
        assert (
            browser.get(
                "/api/v1/knowledge/search", params={"q": "MVP023HTTP", "offset": 1}
            ).json()["total"]
            == 0
        )
        assert browser.get(url).status_code == 200
        before = browser.get(
            "/api/v1/knowledge/search", params={"q": "насос", "offset": 10000}
        ).json()
        with database.unit_of_work() as session:
            assert operation(session, login, pipeline, "revoke", document).ok
        assert browser.get(url).status_code == 404
        assert browser.get(file_url(document, second)).status_code == 404
        assert not any(
            i["id"] == str(document)
            for i in browser.get("/api/v1/documents").json()["items"]
        )
        after = browser.get(
            "/api/v1/knowledge/search", params={"q": "насос", "offset": 10000}
        ).json()
        assert before["total"] == after["total"] and after["items"] == []
        with database.unit_of_work() as session:
            with pytest.raises(SourceNotFound):
                KnowledgeReadService(session, PUBLIC_VIEWER).get_source(part)
        assert (pipeline.root / "documents" / "verified").is_dir()


def test_lifecycle_two_concurrent_publications(
    database: Database, pipeline: FilePipeline, tmp_path: Path
) -> None:
    with database.unit_of_work() as session:
        login, document, first = draft(session, pipeline, tmp_path)
        second = second_draft(session, document, first)
        for version in (first, second):
            proof = approval(session, pipeline, tmp_path, login, document, version)
            assert operation(
                session, login, pipeline, "review", document, version, proof
            ).ok
    start = Event()

    def publish(version: UUID) -> str:
        assert start.wait(5)
        try:
            with database.unit_of_work() as session:
                return operation(
                    session, login, pipeline, "publish", document, version
                ).code
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) != "55P03":
                raise
            return "LOCK_TIMEOUT"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish, v) for v in (first, second)]
        start.set()
        results = [f.result(timeout=15) for f in futures]
        assert "PUBLISHED" in results
        assert set(results) <= {"PUBLISHED", "LOCK_TIMEOUT"}
    # A bounded lock timeout is the existing operator contract. Verify that it
    # leaves exactly one current edition, then explicitly retry in a NEW UoW.
    with database.unit_of_work() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DocumentVersion)
                .where(
                    DocumentVersion.document_id == document,
                    DocumentVersion.status == "published",
                )
            )
            == 1
        )
    for version, result in zip((first, second), results, strict=True):
        if result == "LOCK_TIMEOUT":
            assert publish(version) == "PUBLISHED"
    with database.unit_of_work() as session:
        states = session.scalars(
            select(DocumentVersion.status).where(
                DocumentVersion.document_id == document
            )
        ).all()
        assert sorted(states) == ["published", "retired"]
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == "kb.publish",
                    AuditEvent.entity_id.in_([first, second]),
                    AuditEvent.result == "success",
                )
            )
            == 2
        )


@pytest.mark.parametrize("contender", ["edit", "review", "disable"])
def test_lifecycle_publish_serializes_contenders(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contender: str,
) -> None:
    with database.unit_of_work() as session:
        login, document, version = draft(session, pipeline, tmp_path)
        proof = approval(session, pipeline, tmp_path, login, document, version)
        assert operation(
            session, login, pipeline, "review", document, version, proof
        ).ok
        part = session.scalar(
            select(DocumentSection.id).where(
                DocumentSection.document_version_id == version
            )
        )
        assert part is not None
        disabler = admin(session).login
        user_id = session.scalar(select(StaffUser.id).where(StaffUser.login == login))
        assert user_id is not None
    scanning, finish = Event(), Event()
    original = pipeline.scanner.scan

    def paused(descriptor: int) -> ScanStamp:
        scanning.set()
        assert finish.wait(10)
        return original(descriptor)

    monkeypatch.setattr(pipeline.scanner, "scan", paused)

    def publish() -> str:
        with database.unit_of_work() as session:
            return operation(
                session, login, pipeline, "publish", document, version
            ).code

    def compete() -> str:
        with database.unit_of_work() as session:
            if contender == "review":
                return operation(
                    session, login, pipeline, "review", document, version, proof
                ).code
            if contender == "disable":
                return disable_staff(
                    session,
                    admin_login=disabler,
                    admin_password=PASSWORD,
                    user_id=user_id,
                ).result
            # Raw SQL exercises the PostgreSQL parent-lock trigger even when
            # someone does not use the authenticated draft service.
            try:
                with session.begin_nested():
                    session.execute(
                        text(
                            "UPDATE app.document_sections "
                            "SET text='Concurrent synthetic' "
                            "WHERE id=:id"
                        ),
                        {"id": part},
                    )
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) == "23514":
                    return "IMMUTABLE"
                raise
            return "UNEXPECTED_EDIT"

    def bounded_compete() -> str:
        try:
            return compete()
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) != "55P03":
                raise
            return "LOCK_TIMEOUT"

    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(publish)
        assert scanning.wait(5)
        competing = pool.submit(bounded_compete)
        try:
            # Observe a real PostgreSQL lock waiter, not a timing-only assertion.
            deadline = time.monotonic() + 2
            blocked = False
            while time.monotonic() < deadline:
                with database.engine.connect() as connection:
                    blocked = bool(
                        connection.scalar(
                            text(
                                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                                "WHERE cardinality(pg_blocking_pids(pid)) > 0 "
                                "AND datname=current_database())"
                            )
                        )
                    )
                if blocked:
                    break
            assert blocked and not competing.done()
        finally:
            finish.set()
        assert publishing.result(timeout=10) == "PUBLISHED"
        result = competing.result(timeout=10)
        if result == "LOCK_TIMEOUT":
            result = compete()
        assert (
            result
            == {"edit": "IMMUTABLE", "review": "DRAFT_REQUIRED", "disable": "success"}[
                contender
            ]
        )
    if contender == "disable":
        with database.unit_of_work() as session:
            assert (
                operation(session, login, pipeline, "revoke", document).code
                == "AUTHENTICATION_DENIED"
            )


@pytest.mark.parametrize("change", ["version", "document", "visibility"])
def test_lifecycle_revoke_or_visibility_during_http_scan(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    with database.unit_of_work() as session:
        login, document, version = draft(
            session, pipeline, tmp_path, visibility="public"
        )
        review_publish(
            session, pipeline, tmp_path, login, document, version, public=True
        )
    scanning, finish = Event(), Event()
    original = pipeline.scanner.scan

    def paused(descriptor: int) -> ScanStamp:
        stamp = original(descriptor)
        scanning.set()
        assert finish.wait(10)
        return stamp

    monkeypatch.setattr(pipeline.scanner, "scan", paused)
    with (
        live_http(database, pipeline) as browser,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        download = pool.submit(browser.get, file_url(document, version))
        assert scanning.wait(5)
        try:
            with database.unit_of_work() as session:
                assert operation(
                    session,
                    login,
                    pipeline,
                    "visibility" if change == "visibility" else "revoke",
                    document,
                    version if change == "version" else None,
                    visibility="internal" if change == "visibility" else None,
                ).ok
        finally:
            finish.set()
        response = download.result(timeout=10)
        assert response.status_code == 404
        assert "Synthetic text" not in response.text
        assert browser.get(file_url(document, version)).status_code == 404


def test_lifecycle_commit_error_rolls_back_publication(
    database: Database, pipeline: FilePipeline, tmp_path: Path
) -> None:
    with database.unit_of_work() as session:
        login, document, first = draft(session, pipeline, tmp_path)
        review_publish(session, pipeline, tmp_path, login, document, first)
        second = second_draft(session, document, first)
        proof = approval(session, pipeline, tmp_path, login, document, second)
        assert operation(session, login, pipeline, "review", document, second, proof).ok

    def fail_commit(session: Session) -> None:
        if not session.in_nested_transaction():
            raise SQLAlchemyError("synthetic commit failure")

    event.listen(database.sessions, "before_commit", fail_commit)
    try:
        with pytest.raises(SQLAlchemyError):
            with database.unit_of_work() as session:
                assert operation(
                    session, login, pipeline, "publish", document, second
                ).ok
    finally:
        event.remove(database.sessions, "before_commit", fail_commit)
    with database.unit_of_work() as session:
        assert session.get(DocumentVersion, first).status == "published"  # type: ignore[union-attr]
        assert session.get(DocumentVersion, second).status == "draft"  # type: ignore[union-attr]
        assert not session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "kb.publish",
                AuditEvent.entity_id == second,
                AuditEvent.result == "success",
            )
        )


def cli_request(
    pipeline: FilePipeline,
    login: str,
    arguments: list[str],
    *,
    commit_error: bool = False,
) -> tuple[int, bytes]:
    script = (
        "import sys,runpy;sys.path.insert(0,'/app/tests/integration');"
        "from test_kb_import import SyntheticClamAV;"
        "from pathlib import Path;from app.modules.kb.files import FilePipeline;"
        f"FilePipeline.from_environment=lambda:FilePipeline(Path({str(pipeline.root)!r}),_scanner=SyntheticClamAV(Path({str(pipeline.scanner.database)!r})));"
    )
    if commit_error:
        script += (
            "from sqlalchemy import event;from sqlalchemy.orm import Session;"
            "from sqlalchemy.exc import SQLAlchemyError;"
            "event.listen(Session,'before_commit',lambda s:None "
            "if s.in_nested_transaction() else "
            "(_ for _ in ()).throw(SQLAlchemyError('synthetic commit')));"
        )
    script += "runpy.run_module('app.modules.kb.cli',run_name='__main__')"
    master, slave = pty.openpty()
    child = subprocess.Popen(
        [sys.executable, "-c", script, *arguments],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
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
                if not chunk:
                    break
                output += chunk
                if b"Acting admin login:" in output and not sent_login:
                    os.write(master, (login + "\n").encode())
                    sent_login = True
                if b"Acting admin password:" in output and not sent_password:
                    os.write(master, (PASSWORD + "\n").encode())
                    sent_password = True
        status = child.wait(timeout=2)
        assert PASSWORD.encode() not in output and login.encode() not in output
        return status, output
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        os.close(master)


def test_lifecycle_actual_cli_and_commit_failure(
    database: Database, pipeline: FilePipeline, tmp_path: Path
) -> None:
    with database.unit_of_work() as session:
        login = admin(session).login
        manager = admin(session, role="manager").login
    paths = inputs(tmp_path)
    status, output = cli_request(
        pipeline,
        login,
        ["kb-import", "--file", str(paths[0]), "--manifest", str(paths[1])],
    )
    assert status == 0 and b"IMPORTED_DRAFT" in output
    status, output = cli_request(
        pipeline, login, ["kb-check", "--manifest", str(paths[1])]
    )
    assert status == 0 and b"READY_FOR_MANUAL_REVIEW" in output
    with database.unit_of_work() as session:
        version = version_for(session, paths[1])
        assert version is not None
        document_id, version_id = version.document_id, version.id
        assert version.reviewed_by is None
        proof = approval(
            session, pipeline, tmp_path, login, document_id, version_id, public=True
        )
    target = ["--document", str(document_id), "--version", str(version_id)]
    commands: list[tuple[list[str], str, int, str, bool]] = [
        (["kb-publish", *target], "REVIEW_REQUIRED", 1, login, False),
        (
            ["kb-review", *target, "--evidence", str(proof)],
            "FORBIDDEN",
            1,
            manager,
            False,
        ),
        (["kb-review", *target, "--evidence", str(proof)], "REVIEWED", 0, login, False),
        (["kb-publish", *target], "OPERATION_FAILED_ROLLED_BACK", 1, login, True),
        (["kb-publish", *target], "PUBLISHED", 0, login, False),
        (["kb-preview", *target], "PREVIEW", 0, login, False),
        (
            ["kb-visibility", "--document", str(document_id), "--visibility", "public"],
            "PUBLIC_APPROVAL_REQUIRED",
            1,
            login,
            False,
        ),
        (["kb-retire", *target], "RETIRED", 0, login, False),
        (["kb-revoke", *target], "REVOKED", 0, login, False),
        (["kb-revoke", "--document", str(document_id)], "REVOKED", 0, login, False),
    ]
    for arguments, expected, exit_code, actor_login, commit_error in commands:
        status, output = cli_request(
            pipeline, actor_login, arguments, commit_error=commit_error
        )
        assert status == exit_code and expected.encode() in output
        if commit_error:
            assert b'"code": "PUBLISHED"' not in output
            with database.unit_of_work() as session:
                stored = session.get(DocumentVersion, version_id)
                assert (
                    stored is not None
                    and stored.status == "draft"
                    and stored.published_at is None
                )


def test_lifecycle_substitution_after_success_audit_rolls_back(
    session: Session, pipeline: FilePipeline, tmp_path: Path
) -> None:
    login, document, first = draft(session, pipeline, tmp_path)
    review_publish(session, pipeline, tmp_path, login, document, first)
    second = second_draft(session, document, first)
    proof = approval(session, pipeline, tmp_path, login, document, second)
    assert operation(session, login, pipeline, "review", document, second, proof).ok
    version = session.get(DocumentVersion, second)
    assert version is not None
    path = pipeline.root / "documents" / "verified" / version.file_key
    original = path.read_bytes()

    def substitute(mapper: object, connection: object, target: AuditEvent) -> None:
        if target.action == "kb.publish":
            path.chmod(0o600)
            path.write_bytes(b"Changed after scan and audit\n")
            path.chmod(0o400)

    event.listen(AuditEvent, "after_insert", substitute)
    try:
        assert (
            operation(session, login, pipeline, "publish", document, second).code
            == "FILE_CHANGED"
        )
    finally:
        event.remove(AuditEvent, "after_insert", substitute)
        path.chmod(0o600)
        path.write_bytes(original)
        path.chmod(0o400)
    assert session.get(DocumentVersion, first).status == "published"  # type: ignore[union-attr]
    assert session.get(DocumentVersion, second).status == "draft"  # type: ignore[union-attr]
    assert not session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.action == "kb.publish",
            AuditEvent.entity_id == second,
            AuditEvent.result == "success",
        )
    )


def test_lifecycle_real_http_search_snapshot_and_revoke(
    database: Database, pipeline: FilePipeline, tmp_path: Path
) -> None:
    with database.unit_of_work() as session:
        login, document, version = draft(
            session, pipeline, tmp_path, visibility="public"
        )
        session.execute(
            text(
                "UPDATE app.document_sections SET text='MVP023SEARCH' "
                "WHERE document_version_id=:id"
            ),
            {"id": version},
        )
        review_publish(
            session, pipeline, tmp_path, login, document, version, public=True
        )
    selected, finish = Event(), Event()

    def pause(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if "allowed_matches AS MATERIALIZED" in statement and not selected.is_set():
            selected.set()
            assert finish.wait(10)

    event.listen(database.engine, "after_cursor_execute", pause)
    try:
        with (
            live_http(database, pipeline) as browser,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            search = pool.submit(
                browser.get, "/api/v1/knowledge/search", params={"q": "MVP023SEARCH"}
            )
            assert selected.wait(5)
            try:
                with database.unit_of_work() as session:
                    assert operation(session, login, pipeline, "revoke", document).ok
            finally:
                finish.set()
            # The old query already took its permitted statement snapshot.
            assert search.result(timeout=10).json()["total"] == 1
            for offset in (0, 1, 10000):
                result = browser.get(
                    "/api/v1/knowledge/search",
                    params={"q": "MVP023SEARCH", "offset": offset},
                ).json()
                assert result["total"] == 0 and result["items"] == []
    finally:
        finish.set()
        event.remove(database.engine, "after_cursor_execute", pause)


def test_lifecycle_published_content_remains_immutable(
    session: Session, pipeline: FilePipeline, tmp_path: Path
) -> None:
    login, document, version = draft(session, pipeline, tmp_path)
    review_publish(session, pipeline, tmp_path, login, document, version)
    actor = authenticate_operator(session, login, PASSWORD)
    assert actor is not None
    part = session.scalar(
        select(DocumentSection.id).where(DocumentSection.document_version_id == version)
    )
    assert part is not None
    from app.modules.kb.draft_service import DraftEditRejected

    with pytest.raises(DraftEditRejected):
        DraftEditService(session, actor).edit_section(
            part, SectionContent("Changed", "Changed", None, None, False)
        )
    statements = [
        "UPDATE app.document_versions SET checksum_sha256=repeat('a',64) "
        "WHERE id=:version",
        "UPDATE app.document_versions SET mime='text/markdown' WHERE id=:version",
        "UPDATE app.document_versions SET size_bytes=size_bytes+1 WHERE id=:version",
        "UPDATE app.document_versions SET file_key='different' WHERE id=:version",
        "UPDATE app.document_versions SET approved_at=clock_timestamp() "
        "WHERE id=:version",
        "UPDATE app.document_versions SET reviewed_by=NULL WHERE id=:version",
        "UPDATE app.document_versions SET status='draft' WHERE id=:version",
        "DELETE FROM app.document_versions WHERE id=:version",
        "UPDATE app.document_sections SET title='Changed' WHERE id=:part",
        "UPDATE app.document_sections SET text='Changed' WHERE id=:part",
        "UPDATE app.document_sections SET assistant_allowed=false WHERE id=:part",
        "UPDATE app.document_sections SET section_key='changed' WHERE id=:part",
        "UPDATE app.document_sections SET page_from=1,page_to=1 WHERE id=:part",
        "DELETE FROM app.document_sections WHERE id=:part",
        "INSERT INTO app.document_sections "
        "(id, document_version_id, section_key, title, text) "
        "VALUES (:new,:version,'extra','Extra','Synthetic')",
    ]
    for statement in statements:
        with pytest.raises(SQLAlchemyError):
            with session.begin_nested():
                session.execute(
                    text(statement), {"version": version, "part": part, "new": uuid4()}
                )
    assert operation(session, login, pipeline, "retire", document, version).ok
    with pytest.raises(SQLAlchemyError):
        with session.begin_nested():
            session.execute(
                text("DELETE FROM app.document_versions WHERE id=:id"), {"id": version}
            )
