"""Synthetic PostgreSQL 16 + HTTP + real ClamAV/pipeline ACL acceptance.

The rollback corpus uses real Database UoWs/savepoints, including HTTP commits.
Concurrent tests use independently committed synthetic records in the labelled
test DB; immutable published records and test volumes are deliberately retained.
"""

import io
import os
import secrets
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from pathlib import Path
from threading import Event
from urllib.parse import unquote
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import event, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.auth import hash_password
from app.core.csrf import SESSION_COOKIE
from app.core.db import Database, DatabaseSettings
from app.core.db_types import utc_now
from app.core.staff_models import StaffSession, StaffUser
from app.core.staff_sessions import token_hash
from app.main import app
from app.modules.kb.access import PUBLIC_VIEWER, Viewer
from app.modules.kb.files import FilePipeline, VerifiedFile
from app.modules.kb.files.scanner import ClamAV, ScanStamp
from app.modules.kb.models import Document, DocumentSection, DocumentVersion
from app.modules.kb.read_service import KnowledgeReadService, SourceNotFound, file_url
from app.modules.kb.router import file_pipeline
from app.schemas.sources import KnowledgeQuery

pytestmark = pytest.mark.integration
DATA = "Синтетический насос. Насосы обслуживаются согласно руководству.\n".encode()
WORD = "насос"


class SyntheticClamAV(ClamAV):
    """Existing isolated composition: actual clamscan, never production trust."""

    def _database(self) -> tuple[str, int]:
        epoch = int((self.database / "test.ndb").stat().st_mtime)
        self.require_fresh(epoch)
        return "mvp022-synthetic", epoch


@pytest.fixture
def pipeline(tmp_path: Path) -> FilePipeline:
    signatures = tmp_path / "signatures"
    signatures.mkdir()
    (signatures / "test.ndb").write_text(
        "MVP022.Safe.Test:0:*:" + b"MVP022-SYNTHETIC-INFECTED".hex() + "\n"
    )
    root = Path(os.environ["APP_FILES_DIR"]) / ("synthetic-022-" + uuid4().hex)
    root.mkdir(mode=0o700)
    return FilePipeline(root, _scanner=SyntheticClamAV(signatures))


@dataclass
class Edition:
    document: UUID
    version: UUID
    section: UUID

    @property
    def url(self) -> str:
        return file_url(self.document, self.version)


def edition(
    session: Session,
    pipeline: FilePipeline,
    *,
    visibility: str = "public",
    status: str = "published",
    active: bool = True,
    allowed: bool = True,
    reviewer: UUID | None = None,
    content: str = "Синтетический насос",
    title: str = "Синтетический документ",
    kind: str = "manual",
    data: bytes = DATA,
    extension: str = "txt",
    mime: str = "text/plain",
    fixture_file: VerifiedFile | None = None,
) -> Edition:
    verified = fixture_file or pipeline.accept(
        io.BytesIO(data), filename="test." + extension, mime=mime
    )
    document = Document(
        code="SYNTHETIC-022-" + uuid4().hex,
        title=title,
        kind=kind,
        visibility=visibility,
        status="active" if active else "revoked",
    )
    session.add(document)
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        version=1,
        file_key=verified.file_key,
        mime=verified.mime,
        size_bytes=verified.size_bytes,
        checksum_sha256=verified.checksum_sha256,
        reviewed_by=reviewer,
        approved_at=utc_now() if reviewer else None,
        published_at=utc_now() if reviewer else None,
    )
    session.add(version)
    session.flush()
    section = DocumentSection(
        document_version_id=version.id,
        section_key="intro",
        title="Раздел",
        text=content,
        page_from=None,
        page_to=None,
        assistant_allowed=allowed,
    )
    session.add(section)
    session.flush()
    # Controlled synthetic fixture, never a production publication mechanism.
    if status == "retired":
        version.status = "published"
        session.flush()
    version.status = status
    session.flush()
    return Edition(document.id, version.id, section.id)


@dataclass
class Corpus:
    browser: TestClient
    session: Session
    entries: dict[str, Edition]
    tokens: dict[str, str]
    users: dict[str, UUID]
    sessions: dict[str, UUID]

    def cookie(self, role: str | None) -> None:
        self.browser.cookies.clear()
        if role is not None:
            self.browser.cookies.set(SESSION_COOKIE, self.tokens[role])


@pytest.fixture
def corpus(
    databases: tuple[Database, Database], pipeline: FilePipeline
) -> Iterator[Corpus]:
    with databases[0].engine.connect() as connection, connection.begin() as outer:
        database = Database(DatabaseSettings.from_environment())
        database.sessions = sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        tokens, users, sessions = {}, {}, {}
        with database.unit_of_work() as setup:
            # Earlier schema concurrency tests retain immutable synthetic rows.
            # Hide that corpus only inside this rollback transaction, preserving
            # every original row/state when the fixture exits.
            setup.execute(update(Document).values(status="revoked"))
            password_hash = hash_password("synthetic-022-password")
            for role in ("admin", "manager"):
                user = StaffUser(
                    login="synthetic022-" + uuid4().hex,
                    password_hash=password_hash,
                    role=role,
                )
                setup.add(user)
                setup.flush()
                token = secrets.token_hex(32)
                row = StaffSession(
                    user_id=user.id,
                    token_hash=token_hash(token),
                    expires_at=utc_now() + timedelta(hours=1),
                )
                setup.add(row)
                setup.flush()
                tokens[role], users[role], sessions[role] = token, user.id, row.id
            # Reuse one real verified synthetic file for corpus setup. Each HTTP
            # download independently runs the full prepare/ClamAV boundary.
            verified = pipeline.accept(
                io.BytesIO(DATA), filename="test.txt", mime="text/plain"
            )
            create = partial(edition, setup, pipeline, fixture_file=verified)
            entries = {
                "public": create(),
                "internal": create(visibility="internal", content="насос " * 30),
                "draft": create(status="draft"),
                "internal_draft": create(visibility="internal", status="draft"),
                "retired": create(status="retired", reviewer=users["admin"]),
                "internal_retired": create(
                    status="retired", visibility="internal", reviewer=users["admin"]
                ),
                "unapproved_retired": create(status="retired"),
                "revoked_document": create(active=False),
                "revoked_version": create(status="revoked"),
                "excluded": create(allowed=False),
                "internal_excluded": create(visibility="internal", allowed=False),
            }
        app.dependency_overrides[file_pipeline] = lambda: pipeline
        try:
            with TestClient(app, base_url="https://testserver") as browser:
                app.state.database = database
                with database.unit_of_work() as session:
                    yield Corpus(browser, session, entries, tokens, users, sessions)
        finally:
            app.dependency_overrides.clear()
            database.close()
            outer.rollback()


@pytest.mark.parametrize("role,expected", [(None, 1), ("manager", 2), ("admin", 2)])
@pytest.mark.parametrize("q", ["насос", "насосы", "насосом"])
def test_russian_acl_http(
    corpus: Corpus, role: str | None, expected: int, q: str
) -> None:
    corpus.cookie(role)
    response = corpus.browser.get("/api/v1/knowledge/search", params={"q": q})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == len(body["items"]) == expected
    assert body["limit"] == 5 and body["offset"] == 0
    ids = [hit["source"]["section_id"] for hit in body["items"]]
    assert len(set(ids)) == expected
    assert str(corpus.entries["public"].section) in ids
    if role:
        # Higher-ranked internal hit is present only for a verified staff viewer.
        assert ids[0] == str(corpus.entries["internal"].section)
    else:
        assert str(corpus.entries["internal"].document) not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    for hit in body["items"]:
        source = hit["source"]
        saved = corpus.session.get(DocumentSection, UUID(source["section_id"]))
        assert saved is not None and hit["excerpt"] == saved.text[:600]
        assert source["document_version_id"] == str(saved.document_version_id)
        assert source["url"] == file_url(
            UUID(source["document_id"]), saved.document_version_id
        )
    assert all(
        name not in response.text
        for name in ("file_key", "checksum", "scan", "/var/lib/")
    )


def test_acl_before_page_total_and_equal_ranks(
    corpus: Corpus, pipeline: FilePipeline
) -> None:
    added = [edition(corpus.session, pipeline) for _ in range(3)]
    expected = sorted(
        [str(corpus.entries["public"].section), *(str(e.section) for e in added)]
    )
    seen: list[str] = []
    for offset in range(5):
        response = corpus.browser.get(
            "/api/v1/knowledge/search", params={"q": WORD, "limit": 1, "offset": offset}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 4
        seen.extend(hit["source"]["section_id"] for hit in body["items"])
    assert seen == expected
    corpus.cookie("admin")
    internal = corpus.browser.get(
        "/api/v1/knowledge/search", params={"q": WORD, "limit": 1}
    ).json()
    assert internal["total"] == 5
    assert internal["items"][0]["source"]["section_id"] == str(
        corpus.entries["internal"].section
    )


@pytest.mark.parametrize("query", ["", " ", "\t\n", "несуществующийтермин", '"'])
def test_empty_and_no_match(corpus: Corpus, query: str) -> None:
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _params: object,
        _context: object,
        _many: bool,
    ) -> None:
        statements.append(statement)

    event.listen(corpus.session.get_bind(), "before_cursor_execute", capture)
    try:
        result = corpus.browser.get(
            "/api/v1/knowledge/search", params={"q": query}
        ).json()
    finally:
        event.remove(corpus.session.get_bind(), "before_cursor_execute", capture)
    assert result == {"items": [], "total": 0, "limit": 5, "offset": 0}
    if not query.strip():
        assert not any(
            "websearch_to_tsquery" in sql or "ts_rank_cd" in sql for sql in statements
        )


@pytest.mark.parametrize(
    "params",
    [
        {"q": "x" * 1001},
        {"q": WORD, "limit": 0},
        {"q": WORD, "limit": 6},
        {"q": WORD, "offset": -1},
        {"q": WORD, "offset": 10001},
        {"q": WORD, "limit": "nan"},
        {"q": WORD, "role": "admin"},
        {"q": WORD, "visibility": "internal"},
        {"q": WORD, "actor_id": str(uuid4())},
    ],
)
def test_bad_search_parameters(corpus: Corpus, params: dict[str, str | int]) -> None:
    response = corpus.browser.get("/api/v1/knowledge/search", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["details"] == {}
    assert "насос" not in response.text


@pytest.mark.parametrize("role,count", [(None, 2), ("manager", 4), ("admin", 4)])
def test_document_list(corpus: Corpus, role: str | None, count: int) -> None:
    corpus.cookie(role)
    body = corpus.browser.get("/api/v1/documents").json()
    assert body["total"] == len(body["items"]) == count
    assert body["limit"] == 20
    assert {item["document_version_id"] for item in body["items"]} == {
        str(corpus.entries[key].version)
        for key in (
            ("public", "excluded", "internal", "internal_excluded")
            if role
            else ("public", "excluded")
        )
    }
    assert corpus.browser.get("/api/v1/documents?offset=10000").json()["total"] == count
    assert corpus.browser.get("/api/v1/documents?kind=article").json()["total"] == 0
    for query in ("kind=invalid", "limit=101", "offset=10001", "is_admin=true"):
        assert corpus.browser.get("/api/v1/documents?" + query).status_code == 422


@pytest.mark.parametrize("role", [None, "manager", "admin"])
def test_files_exact_acl_and_headers(corpus: Corpus, role: str | None) -> None:
    corpus.cookie(role)
    for key, entry in corpus.entries.items():
        accessible = key in {"public", "excluded", "retired"} or (
            role is not None
            and key in {"internal", "internal_excluded", "internal_retired"}
        )
        response = corpus.browser.get(entry.url)
        assert response.status_code == (200 if accessible else 404), (
            key,
            response.text,
        )
        if accessible:
            assert response.content == DATA
            assert response.headers["content-type"].startswith("text/plain")
            assert response.headers["content-disposition"].startswith("attachment;")
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["vary"] == "Cookie"
            assert response.headers["x-document-version-status"] == (
                "retired" if key in {"retired", "internal_retired"} else "published"
            )
            if key in {"retired", "internal_retired"}:
                assert "предыдущая редакция" in unquote(
                    response.headers["content-disposition"]
                )
        else:
            assert response.json()["error"]["details"] == {}
    a, b = corpus.entries["public"], corpus.entries["internal"]
    mismatch = corpus.browser.get(file_url(a.document, b.version))
    unknown = corpus.browser.get(file_url(uuid4(), uuid4()))
    assert mismatch.status_code == unknown.status_code == 404
    assert mismatch.json()["error"] == unknown.json()["error"]


def test_sources_revalidate_and_chat_ignores_staff(corpus: Corpus) -> None:
    corpus.cookie("admin")
    public = KnowledgeReadService(corpus.session, PUBLIC_VIEWER)
    staff = KnowledgeReadService(corpus.session, Viewer(corpus.sessions["admin"]))
    assert public.search(KnowledgeQuery(q=WORD)).total == 1
    assert staff.search(KnowledgeQuery(q=WORD)).total == 2
    for key in (
        "internal",
        "draft",
        "internal_draft",
        "internal_retired",
        "unapproved_retired",
        "revoked_document",
        "revoked_version",
        "excluded",
    ):
        with pytest.raises(SourceNotFound):
            public.get_source(corpus.entries[key].section)
    with pytest.raises(SourceNotFound):
        public.get_source(uuid4())
    retired = public.get_source(corpus.entries["retired"].section)
    assert "предыдущая редакция" in retired.title
    entry = corpus.entries["public"]
    old_source = public.get_source(entry.section)
    corpus.session.execute(
        update(Document)
        .where(Document.id == entry.document)
        .values(visibility="internal")
    )
    with pytest.raises(SourceNotFound):
        public.get_source(entry.section)
    assert corpus.browser.get(old_source.url).status_code == 200  # verified admin
    corpus.cookie(None)
    assert corpus.browser.get(old_source.url).status_code == 404
    assert public.search(KnowledgeQuery(q=WORD)).total == 0
    corpus.session.execute(
        update(Document).where(Document.id == entry.document).values(status="revoked")
    )
    with pytest.raises(SourceNotFound):
        staff.get_source(entry.section)
    corpus.cookie("admin")
    assert corpus.browser.get(old_source.url).status_code == 404


def test_current_published_with_old_and_draft_editions(
    corpus: Corpus, pipeline: FilePipeline
) -> None:
    previous = corpus.entries["retired"]
    verified = pipeline.accept(
        io.BytesIO(b"Synthetic current edition"),
        filename="current.txt",
        mime="text/plain",
    )
    versions: list[DocumentVersion] = []
    for number in (2, 3):
        version = DocumentVersion(
            document_id=previous.document,
            version=number,
            file_key=verified.file_key,
            mime=verified.mime,
            size_bytes=verified.size_bytes,
            checksum_sha256=verified.checksum_sha256,
        )
        corpus.session.add(version)
        corpus.session.flush()
        corpus.session.add(
            DocumentSection(
                document_version_id=version.id,
                section_key="intro",
                title="Раздел",
                text="Синтетический насос",
                assistant_allowed=True,
                page_from=None,
                page_to=None,
            )
        )
        corpus.session.flush()
        if number == 2:
            version.status = "published"
            corpus.session.flush()
        versions.append(version)
    listing = corpus.browser.get("/api/v1/documents").json()
    item = next(row for row in listing["items"] if row["id"] == str(previous.document))
    assert item["document_version_id"] == str(versions[0].id) and item["version"] == 2
    search = corpus.browser.get("/api/v1/knowledge/search", params={"q": WORD}).json()
    version_ids = {hit["source"]["document_version_id"] for hit in search["items"]}
    assert str(versions[0].id) in version_ids
    assert (
        str(previous.version) not in version_ids
        and str(versions[1].id) not in version_ids
    )
    assert corpus.browser.get(previous.url).content == DATA
    assert corpus.browser.get(item["url"]).content == b"Synthetic current edition"
    assert (
        corpus.browser.get(file_url(previous.document, versions[1].id)).status_code
        == 404
    )


@pytest.mark.parametrize(
    "fault",
    ["invalid", "expired", "logout", "disabled", "role_changed", "forged_actor"],
)
def test_staff_loses_access(corpus: Corpus, fault: str) -> None:
    corpus.cookie("admin")
    if fault == "invalid":
        corpus.browser.cookies.set(SESSION_COOKIE, secrets.token_hex(32))
    elif fault in {"expired", "logout"}:
        corpus.session.execute(
            update(StaffSession)
            .where(StaffSession.id == corpus.sessions["admin"])
            .values(
                **(
                    {"expires_at": utc_now() - timedelta(seconds=1)}
                    if fault == "expired"
                    else {"revoked_at": utc_now()}
                )
            )
        )
    elif fault == "disabled":
        corpus.session.execute(
            update(StaffUser)
            .where(StaffUser.id == corpus.users["admin"])
            .values(is_active=False)
        )
    elif fault == "role_changed":
        # Manager is still an explicitly permitted role; no stale admin identity.
        corpus.session.execute(
            update(StaffUser)
            .where(StaffUser.id == corpus.users["admin"])
            .values(role="manager")
        )
    else:
        corpus.cookie(None)
        corpus.browser.headers.update(
            {
                "X-Actor-ID": str(corpus.users["admin"]),
                "X-Role": "admin",
                "X-Is-Admin": "true",
            }
        )
    expected = 2 if fault == "role_changed" else 1
    assert (
        corpus.browser.get("/api/v1/knowledge/search", params={"q": WORD}).json()[
            "total"
        ]
        == expected
    )
    assert corpus.browser.get(corpus.entries["internal"].url).status_code == (
        200 if expected == 2 else 404
    )


def test_xss_is_exact_bounded_json(corpus: Corpus, pipeline: FilePipeline) -> None:
    payload = (
        '<script>alert("synthetic")</script><img src=x onerror=alert(1)> '
        "[link](javascript:alert(1)) насос"
    )
    entry = edition(corpus.session, pipeline, content=payload * 20, title=payload[:200])
    response = corpus.browser.get("/api/v1/knowledge/search", params={"q": WORD})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    hit = next(
        item
        for item in response.json()["items"]
        if item["source"]["section_id"] == str(entry.section)
    )
    assert hit["excerpt"] == (payload * 20)[:600]
    assert hit["source"]["title"] == payload[:200]
    # JSON strings are plain text. No HTML renderer or ts_headline markup is used.
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "extension,mime",
    [("txt", "text/plain"), ("md", "text/markdown"), ("pdf", "application/pdf")],
)
def test_complete_snapshot_response(
    corpus: Corpus, pipeline: FilePipeline, extension: str, mime: str
) -> None:
    data = DATA
    if extension == "pdf":
        output = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(72, 72)
        writer.write(output)
        data = output.getvalue()
    entry = edition(corpus.session, pipeline, data=data, extension=extension, mime=mime)
    response = corpus.browser.get(entry.url)
    # The prepare owner has closed its fd by now; the complete response owns bytes.
    assert response.status_code == 200 and response.content == data
    assert int(response.headers["content-length"]) == len(data)
    assert response.headers["content-type"].split(";")[0] == mime


def test_real_query_ranking_fence_and_gin(corpus: Corpus) -> None:
    captured: list[tuple[str, dict[str, object]]] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: dict[str, object],
        _context: object,
        _many: bool,
    ) -> None:
        captured.append((statement, parameters))

    connection = corpus.session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        result = KnowledgeReadService(corpus.session, PUBLIC_VIEWER).search(
            KnowledgeQuery(q=WORD)
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert result.total == 1 and len(captured) == 1
    statement, parameters = captured[0]
    assert statement.index("MATERIALIZED") < statement.index("ts_rank_cd")
    assert "allowed_matches" in statement and "websearch_to_tsquery" in statement
    assert "assistant_allowed IS true" in statement
    assert "published" in parameters.values() and "public" in parameters.values()
    assert WORD not in statement and WORD in parameters.values()
    connection.exec_driver_sql("SET LOCAL enable_seqscan=off")
    # This small corpus can use a selective FK B-tree instead. Demonstrate
    # that the actual ACL query also has a GIN path, without promising its cost
    # wins on tiny tables or changing any runtime planner settings.
    connection.exec_driver_sql("SET LOCAL enable_indexscan=off")
    connection.exec_driver_sql("SET LOCAL enable_nestloop=off")
    plan = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE, FORMAT JSON) " + statement, parameters
    ).scalar_one()
    assert "ix_document_sections_search_vector" in str(plan)


def test_session_expiring_during_scan(
    corpus: Corpus, pipeline: FilePipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus.cookie("admin")
    scan = pipeline.scanner.scan
    # Start the fixture's savepoint before the HTTP UoW nests inside it.
    connection = corpus.session.connection()

    def expire(descriptor: int) -> ScanStamp:
        result = scan(descriptor)
        connection.execute(
            update(StaffSession)
            .where(StaffSession.id == corpus.sessions["admin"])
            .values(expires_at=utc_now() - timedelta(seconds=1))
        )
        return result

    monkeypatch.setattr(pipeline.scanner, "scan", expire)
    response = corpus.browser.get(corpus.entries["internal"].url)
    assert response.status_code == 404 and DATA not in response.content


@pytest.mark.parametrize(
    "fault",
    [
        "tampered",
        "symlink",
        "missing",
        "stale_av",
        "missing_av",
        "unsigned_av",
        "checksum",
        "traversal",
    ],
)
def test_download_fails_before_success(
    corpus: Corpus, pipeline: FilePipeline, tmp_path: Path, fault: str
) -> None:
    entry = corpus.entries["public"]
    version = corpus.session.get(DocumentVersion, entry.version)
    assert version is not None
    target = pipeline.root / "documents" / "verified" / version.file_key
    if fault == "tampered":
        target.chmod(0o600)
        target.write_bytes(b"synthetic replaced")
        target.chmod(0o400)
    elif fault == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "outside")
    elif fault == "missing":
        target.unlink()
    elif fault == "stale_av":
        os.utime(pipeline.scanner.database / "test.ndb", (1, 1))
    elif fault in {"missing_av", "unsigned_av"}:
        directory = tmp_path / "production-bases"
        directory.mkdir()
        if fault == "unsigned_av":
            for name in ("main.cvd", "daily.cvd", "bytecode.cvd"):
                (directory / name).write_bytes(b"ClamAV-VDB:synthetic".ljust(512, b" "))
        app.dependency_overrides[file_pipeline] = lambda: FilePipeline(
            pipeline.root, _scanner=ClamAV(directory)
        )
    else:
        # A different DB binding must never authorize the clean receipt/file.
        # Use a separate synthetic draft then establish its test-only state.
        draft = edition(corpus.session, pipeline, status="draft")
        row = corpus.session.get(DocumentVersion, draft.version)
        assert row is not None
        if fault == "checksum":
            row.checksum_sha256 = "a" * 64
        else:
            # Schema blocks slash traversal; a schema-valid non-server key is
            # also rejected by the stricter file boundary without opening it.
            row.file_key = "synthetic_non_server_key"
        row.status = "published"
        corpus.session.flush()
        entry = draft
    response = corpus.browser.get(entry.url)
    assert response.status_code == 503, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in response.headers
    assert response.json()["error"]["details"] == {}
    assert DATA not in response.content
    assert version.file_key not in response.text


def test_real_scanner_failure_and_exit_mutation(
    corpus: Corpus, pipeline: FilePipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.kb.files import scanner as scanner_module
    from app.modules.kb.files.process import Limits
    from app.modules.kb.files.sandbox import run_tool

    original = run_tool

    def unavailable(
        command: list[str], *, fds: tuple[int, ...], limits: Limits
    ) -> bytes:
        if command[0] == "/usr/bin/clamscan":
            command = ["/missing-clamscan"]
        return original(command, fds=fds, limits=limits)

    monkeypatch.setattr(scanner_module, "run_tool", unavailable)
    assert corpus.browser.get(corpus.entries["public"].url).status_code == 503
    monkeypatch.undo()
    scan = pipeline.scanner.scan
    version = corpus.session.get(DocumentVersion, corpus.entries["public"].version)
    assert version is not None
    target = pipeline.root / "documents" / "verified" / version.file_key

    def mutation(descriptor: int) -> ScanStamp:
        stamp = scan(descriptor)
        target.chmod(0o600)
        target.write_bytes(b"synthetic changed after actual AV")
        target.chmod(0o400)
        return stamp

    monkeypatch.setattr(pipeline.scanner, "scan", mutation)
    response = corpus.browser.get(corpus.entries["public"].url)
    assert response.status_code == 503 and DATA not in response.content


@pytest.mark.parametrize(
    "change", ["visibility", "document_revoked", "version_revoked"]
)
def test_concurrent_change_during_real_scan(
    databases: tuple[Database, Database],
    pipeline: FilePipeline,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    database = databases[0]
    with database.unit_of_work() as session:
        entry = edition(session, pipeline, content="Синтетический конкурентный насос")
    started, proceed = Event(), Event()
    scan = pipeline.scanner.scan

    def pause(descriptor: int) -> ScanStamp:
        result = scan(descriptor)
        started.set()
        assert proceed.wait(5)
        return result

    monkeypatch.setattr(pipeline.scanner, "scan", pause)
    app.dependency_overrides[file_pipeline] = lambda: pipeline
    try:
        with TestClient(app, base_url="https://testserver") as browser:
            app.state.database = database
            with ThreadPoolExecutor(1) as executor:
                pending = executor.submit(browser.get, entry.url)
                try:
                    assert started.wait(5)
                    with database.unit_of_work() as session:
                        if change == "version_revoked":
                            session.execute(
                                update(DocumentVersion)
                                .where(DocumentVersion.id == entry.version)
                                .values(status="revoked")
                            )
                        else:
                            session.execute(
                                update(Document)
                                .where(Document.id == entry.document)
                                .values(
                                    **(
                                        {"visibility": "internal"}
                                        if change == "visibility"
                                        else {"status": "revoked"}
                                    )
                                )
                            )
                finally:
                    proceed.set()
                response = pending.result(timeout=10)
            assert response.status_code == 404 and DATA not in response.content
            assert browser.get(entry.url).status_code == 404
            body = browser.get(
                "/api/v1/knowledge/search", params={"q": "конкурентный"}
            ).json()
            assert str(entry.section) not in str(body)
            assert str(entry.document) not in browser.get("/api/v1/documents").text
    finally:
        proceed.set()
        app.dependency_overrides.clear()
