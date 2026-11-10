"""Locked SYNTHETIC ONLY corpus; actual PG16, domain lifecycle, HTTP and chat."""

import io
import json
import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_kb_import import PASSWORD, SyntheticClamAV, admin

from app.core.auth import AuthenticatedStaff
from app.core.csrf import SESSION_COOKIE, StaffSecurity
from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff import authenticate_operator
from app.core.staff_models import StaffSession, StaffUser
from app.core.staff_sessions import token_hash
from app.main import app
from app.modules.chat.mode_router import mode_buttons, specialist
from app.modules.kb.access import PUBLIC_VIEWER
from app.modules.kb.draft_service import (
    DraftDocument,
    DraftEditService,
    DraftFile,
    KeyedSection,
    SectionContent,
)
from app.modules.kb.files import FilePipeline
from app.modules.kb.lifecycle_service import LifecycleService
from app.modules.kb.models import Document, DocumentSection, DocumentVersion
from app.modules.kb.read_service import KnowledgeReadService, SourceNotFound, source_ref
from app.modules.kb.router import file_pipeline
from app.schemas.chat import ChatReply, OpenDocumentAction
from app.schemas.sources import KnowledgeHit, KnowledgePage, KnowledgeQuery
from kb_acceptance.corpus import Corpus, CorpusRejected, load_synthetic
from kb_acceptance.external import Link, existing_mapping
from kb_acceptance.measure import (
    NO_MATCH,
    Binding,
    Observation,
    measure,
    quoted_text,
    verify_public_source,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[1] / "fixtures" / "kb_acceptance"
ORIGIN = "https://localhost:8443"


@dataclass
class Prepared:
    database: Database
    pipeline: FilePipeline
    corpus: Corpus
    mapping: dict[str, Binding]
    admin_login: str
    admin_id: UUID


def bind(
    session: Session, corpus: Corpus, versions: dict[tuple[str, int], UUID]
) -> dict[str, Binding]:
    result = {}
    for spec in corpus.sections:
        version = session.get(
            DocumentVersion, versions[spec.document_key, spec.version]
        )
        assert version is not None
        document = session.get(Document, version.document_id)
        assert document is not None
        section = session.scalar(
            select(DocumentSection).where(
                DocumentSection.document_version_id == version.id,
                DocumentSection.section_key == spec.section_key,
            )
        )
        assert section is not None
        result[spec.key] = Binding(
            spec.document_key,
            spec.version,
            KnowledgeHit(
                excerpt=section.text[:600],
                source=source_ref(document, version, section),
            ),
        )
    return result


def publish_synthetic(
    lifecycle: LifecycleService,
    version: DocumentVersion,
    directory: Path,
    *,
    public: bool,
) -> None:
    preview = lifecycle.execute("preview", version.document_id)
    entries: Any = preview.technical["versions"]
    entry = next(e for e in entries if e["version_id"] == str(version.id))
    evidence = directory / (uuid4().hex + ".json")
    evidence.write_text(
        json.dumps(
            {
                **{
                    k: entry[k]
                    for k in (
                        "version_id",
                        "checksum_sha256",
                        "file_binding_sha256",
                        "sections_sha256",
                    )
                },
                "expert_name": "SYNTHETIC ONLY Expert fixture",
                "expert_reference": "SYNTHETIC-ONLY:047:FIXTURE",
                "expert_approved_on": datetime.now(UTC).date().isoformat(),
                "decision": "approved",
                "content_checked": True,
                "full_file_public_allowed": public,
                **(
                    {"public_release_reference": "SYNTHETIC-ONLY:047:FILE"}
                    if public
                    else {}
                ),
            }
        )
    )
    evidence.chmod(0o600)
    assert (
        lifecycle.execute(
            "review",
            version.document_id,
            version_id=version.id,
            evidence_path=evidence,
        ).code
        == "REVIEWED"
    )
    assert (
        lifecycle.execute("publish", version.document_id, version_id=version.id).code
        == "PUBLISHED"
    )


@pytest.fixture(scope="module")
def prepared(
    databases: tuple[Database, Database], tmp_path_factory: pytest.TempPathFactory
) -> Prepared:
    directory = tmp_path_factory.mktemp("kb047-synthetic-only")
    signatures = directory / "signatures"
    signatures.mkdir()
    (signatures / "test.ndb").write_text(
        "MVP047.Synthetic.Only:0:*:" + b"MVP047-INFECTED".hex() + "\n"
    )
    root = Path(os.environ["APP_FILES_DIR"]) / ("synthetic-only-047-" + uuid4().hex)
    root.mkdir(mode=0o700)
    pipeline = FilePipeline(root, _scanner=SyntheticClamAV(signatures))
    corpus = load_synthetic(ROOT)
    database = databases[0]
    versions: dict[tuple[str, int], UUID] = {}
    with database.unit_of_work() as session:
        user = admin(session)
        login, identifier = user.login, user.id
        actor = authenticate_operator(session, login, PASSWORD)
        assert actor is not None
        session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '180s'"))
        drafts = DraftEditService(session, actor)
        lifecycle = LifecycleService(session, actor, pipeline)
        for spec in corpus.manifest.documents:
            data = (ROOT / spec.material).read_bytes()
            accepted = pipeline.accept(
                io.BytesIO(data), filename="synthetic.txt", mime="text/plain"
            )
            version = drafts.import_draft(
                DraftDocument(
                    "SYNTHETIC-047-" + spec.key, spec.title, "other", spec.visibility
                ),
                spec.version,
                DraftFile(
                    accepted.file_key,
                    accepted.mime,
                    accepted.size_bytes,
                    accepted.checksum_sha256,
                ),
            )
            parts = [
                s
                for s in corpus.sections
                if (s.document_key, s.version) == (spec.key, spec.version)
            ]
            drafts.reconcile_sections(
                version.id,
                [
                    KeyedSection(
                        s.section_key,
                        SectionContent(
                            s.title, s.text, s.page_from, s.page_to, s.assistant_allowed
                        ),
                    )
                    for s in parts
                ],
            )
            versions[spec.key, spec.version] = version.id
            if spec.version_status != "draft":
                publish_synthetic(
                    lifecycle, version, directory, public=spec.visibility == "public"
                )
                if spec.version_status in {"retired", "revoked"}:
                    assert lifecycle.execute(
                        "retire" if spec.version_status == "retired" else "revoke",
                        version.document_id,
                        version_id=version.id,
                    ).ok
                if spec.status == "revoked":
                    assert lifecycle.execute("revoke", version.document_id).ok
        mapping = bind(session, corpus, versions)
    return Prepared(database, pipeline, corpus, mapping, login, identifier)


@pytest.fixture
def browser(prepared: Prepared) -> Iterator[TestClient]:
    app.dependency_overrides[file_pipeline] = lambda: prepared.pipeline
    try:
        with TestClient(app, client=("192.0.2.1", 50000), base_url=ORIGIN) as client:
            app.state.database = prepared.database
            app.state.staff_security = StaffSecurity(
                frozenset({ORIGIN}), secrets.token_hex(32)
            )
            yield client
    finally:
        app.dependency_overrides.clear()


class ChatHTTPError(Exception):
    pass


def chat(
    client: TestClient,
    query: str,
    owner: dict[str, Any] | None = None,
    version: int = 0,
) -> tuple[ChatReply, dict[str, Any], dict[str, Any]]:
    if owner is None:
        response = client.post("/api/v1/chat/sessions", json={"mode": "knowledge"})
        if response.status_code != 201:
            raise ChatHTTPError
        owner = response.json()
    assert owner is not None
    request = {
        "session_id": owner["session_id"],
        "request_id": str(uuid4()),
        "expected_version": version,
        "input": {"type": "text", "value": query},
    }
    response = client.post(
        "/api/v1/chat/messages",
        json=request,
        headers={"X-Chat-Token": owner["session_token"]},
    )
    if response.status_code != 200:
        raise ChatHTTPError
    return ChatReply.model_validate(response.json()["reply"]), owner, request


def collect(
    prepared: Prepared, browser: TestClient, tokens: list[str]
) -> list[Observation]:
    observations = []
    owner = None
    for index, query in enumerate(prepared.corpus.queries):
        if index % 20 == 0:
            owner = None
        # Error is an invalid observation, never a successful empty search.
        try:
            with prepared.database.unit_of_work() as session:
                page = KnowledgeReadService(session, PUBLIC_VIEWER).search(
                    KnowledgeQuery(q=query.text)
                )
        except SQLAlchemyError:
            observations.append(
                Observation(query.query_id, error=True, error_kind="db")
            )
            continue
        response = browser.get("/api/v1/knowledge/search", params={"q": query.text})
        if response.status_code != 200:
            observations.append(
                Observation(query.query_id, page, error=True, error_kind="http")
            )
            continue
        assert set(response.json()) == {"items", "total", "limit", "offset"}
        assert response.headers["cache-control"] == "no-store"
        try:
            reply, owner, _ = chat(browser, query.text, owner, index % 20)
        except ChatHTTPError:
            observations.append(
                Observation(query.query_id, page, error=True, error_kind="http")
            )
            continue
        tokens.append(owner["session_token"])
        observations.append(
            Observation(
                query.query_id,
                page,
                reply,
                http_matches=KnowledgePage.model_validate(response.json()) == page,
            )
        )
    return observations


def test_complete_locked_synthetic_corpus(
    prepared: Prepared, browser: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    tokens: list[str] = []
    links = {
        key: Link(
            document_code="SYNTHETIC-047-" + binding.document_key,
            document_id=str(binding.hit.source.document_id),
            document_version_id=str(binding.hit.source.document_version_id),
            section_id=str(binding.hit.source.section_id),
        )
        for key, binding in prepared.mapping.items()
    }
    with prepared.database.unit_of_work() as session:
        assert (
            existing_mapping(session, prepared.corpus, links, prepared.pipeline)
            == prepared.mapping
        )
        for field in ("document_id", "document_version_id", "section_id"):
            bad = dict(links)
            bad["indicator"] = links["indicator"].model_copy(
                update={field: getattr(links["internal"], field)}
            )
            with pytest.raises(CorpusRejected, match="WRONG_DATABASE_MAPPING"):
                existing_mapping(session, prepared.corpus, bad, prepared.pipeline)
    observations = collect(prepared, browser, tokens)
    report = measure(prepared.corpus, prepared.mapping, observations)
    with prepared.database.unit_of_work() as session:
        report["postgresql_version"] = session.scalar(text("SHOW server_version"))
        report["measured_schema_revisions"] = [
            session.scalar(text("SELECT version_num FROM app.alembic_version"))
        ]
    report["head_sha"] = os.environ.get("KB_ACCEPTANCE_HEAD", "UNRECORDED")
    report["dirty"] = os.environ.get("KB_ACCEPTANCE_DIRTY", "unknown")
    report["environment"] = prepared.corpus.manifest.environment.model_dump()
    report["mapping"] = {
        key: binding.hit.source.model_dump(mode="json")
        for key, binding in prepared.mapping.items()
    }
    private = prepared.pipeline.root / "measurement.json"
    with private.open("x") as output:
        private.chmod(0o600)
        json.dump(report, output, ensure_ascii=False, indent=2)
    # Explicit allowlist; no text, credentials, expert identities or document bytes.
    fields = (
        "format",
        "corpus_version",
        "corpus_sha256",
        "relevant_count",
        "negative_count",
        "expected_section_count",
        "hits",
        "misses",
        "hit_rate_at_5",
        "SYNTHETIC_HIT_RATE_AT_5",
        "mrr_at_5",
        "negative_passed",
        "acl_incidents",
        "page_acl_incidents",
        "reply_acl_incidents",
        "complete",
        "faults",
        "technical_result",
        "SYNTHETIC_CORPUS_VALIDATION",
        "SYNTHETIC_NEGATIVE_SAFETY",
        "REAL_EXPERT_CORPUS",
        "REAL_TOP5_ACCEPTANCE",
        "REAL_HIT_RATE_AT_5",
        "REAL_NEGATIVE_SAFETY",
        "EXPERT_REVIEW",
        "TARGET_STAGING",
        "PRODUCTION_ADMISSION",
        "postgresql_version",
        "head_sha",
        "dirty",
    )
    aggregate = {key: report[key] for key in fields}
    # Synthetic query IDs and logical keys are public fixture evidence.
    aggregate["queries"] = report["queries"]
    for key in (
        "total_relevant",
        "hit_at_5_count",
        "miss_at_5_count",
        "total_negative",
        "negative_safe_count",
        "negative_unsafe_count",
        "failed_query_ids",
        "http_error_count",
        "db_error_count",
        "query_error_count",
        "missing_result_count",
        "measured_corpus_versions",
        "measured_schema_revisions",
    ):
        aggregate[key] = report[key]
    for protected in (
        *tokens,
        PASSWORD,
        prepared.admin_login,
        *(q.text for q in prepared.corpus.queries),
        *(s.text for s in prepared.corpus.sections),
    ):
        assert protected not in caplog.text
        assert protected not in json.dumps(aggregate, ensure_ascii=False)
    print("KB_ACCEPTANCE_JSON " + json.dumps(aggregate, ensure_ascii=False))
    assert report["technical_result"] == "PASS"


def test_hidden_competitors_do_not_change_any_public_top5_total_or_offset(
    prepared: Prepared, browser: TestClient
) -> None:
    queries = [q for q in prepared.corpus.queries if q.group == "relevant"]
    with prepared.database.unit_of_work() as session:
        reader = KnowledgeReadService(session, PUBLIC_VIEWER)
        before = [reader.search(KnowledgeQuery(q=q.text)) for q in queries]
        # Rollback-only synthetic fixture adds high-density hidden hits. Every
        # section is created through the draft domain service; never rank mocks.
        with session.begin_nested() as transaction:
            user = session.get(StaffUser, prepared.admin_id)
            assert user is not None
            service = DraftEditService(session, AuthenticatedStaff(user))
            original = session.get(
                DocumentVersion,
                prepared.mapping["indicator"].hit.source.document_version_id,
            )
            assert original is not None
            version = service.import_draft(
                DraftDocument(
                    "SYNTHETIC-047-HIDDEN",
                    "SYNTHETIC ONLY hidden competitors",
                    "other",
                    "internal",
                ),
                1,
                DraftFile(
                    original.file_key,
                    original.mime,
                    original.size_bytes,
                    original.checksum_sha256,
                ),
            )
            service.reconcile_sections(
                version.id,
                [
                    KeyedSection(
                        f"noise-{i}",
                        SectionContent(
                            "SYNTHETIC ONLY",
                            "SYNTHETIC ONLY. " + " ".join(q.text for q in queries) * 12,
                            None,
                            None,
                            True,
                        ),
                    )
                    for i in range(12)
                ],
            )
            # Explicit fixture-only publication state, in guarded synthetic DB.
            # Normative review/publish is exercised by the locked corpus setup.
            version.status = "published"
            session.flush()
            for q, expected in zip(queries, before, strict=True):
                assert reader.search(KnowledgeQuery(q=q.text)) == expected
                for offset in (0, 1, 4, 5, 10000):
                    page = reader.search(
                        KnowledgeQuery(q=q.text, limit=1, offset=offset)
                    )
                    assert page.total == expected.total
                    # For the top five this is exactly the same ordered selection.
                    if offset < 5:
                        assert page.items == expected.items[offset : offset + 1]
                assert reader.search(KnowledgeQuery(q=q.text)) == expected
            transaction.rollback()


@pytest.mark.parametrize("role", ["admin", "manager"])
@pytest.mark.parametrize("loss", ["logout", "disabled", "expired"])
def test_staff_session_loss_and_public_chat(
    prepared: Prepared, browser: TestClient, role: str, loss: str
) -> None:
    token = secrets.token_hex(32)
    with prepared.database.unit_of_work() as session:
        user = admin(session, role=role)
        staff = StaffSession(
            user_id=user.id,
            token_hash=token_hash(token),
            expires_at=utc_now() + timedelta(hours=1),
        )
        session.add(staff)
        session.flush()
        user_id, staff_id = user.id, staff.id
    browser.cookies.set(SESSION_COOKIE, token)
    params = {"q": "закрытометка"}
    found = browser.get("/api/v1/knowledge/search", params=params)
    assert found.status_code == 200 and found.json()["total"] == 1
    assert chat(browser, params["q"])[0].resolution == "no_match"
    with prepared.database.unit_of_work() as session:
        if loss == "disabled":
            session.execute(
                update(StaffUser).where(StaffUser.id == user_id).values(is_active=False)
            )
        else:
            session.execute(
                update(StaffSession)
                .where(StaffSession.id == staff_id)
                .values(
                    **(
                        {"revoked_at": utc_now()}
                        if loss == "logout"
                        else {"expires_at": utc_now() - timedelta(seconds=1)}
                    )
                )
            )
    result = browser.get("/api/v1/knowledge/search", params=params)
    assert result.status_code == 200 and result.json()["total"] == 0
    assert browser.get(prepared.mapping["internal"].hit.source.url).status_code == 404
    assert chat(browser, params["q"])[0].resolution == "no_match"


def measurement_reply(page: KnowledgePage) -> ChatReply:
    """A consistent response for measurement fault injection, not FTS evidence."""
    return ChatReply(
        mode="knowledge",
        text=quoted_text(page) if page.items else NO_MATCH,
        buttons=mode_buttons(),
        sources=[hit.source for hit in page.items],
        items=[],
        actions=[
            OpenDocumentAction(type="open_document", payload=hit.source)
            for hit in page.items
        ]
        if page.items
        else [specialist()],
        resolution="results" if page.items else "no_match",
    )


@pytest.fixture(scope="module")
def public_competitor(
    prepared: Prepared, tmp_path_factory: pytest.TempPathFactory
) -> KnowledgeHit:
    """Extra DB content, outside the unchanged locked corpus and its mapping."""
    directory = tmp_path_factory.mktemp("kb047-extra-synthetic-only")
    with prepared.database.unit_of_work() as session:
        actor = authenticate_operator(session, prepared.admin_login, PASSWORD)
        assert actor is not None
        data = "SYNTHETIC ONLY. Постороннеметка постороннеметка.".encode()
        accepted = prepared.pipeline.accept(
            io.BytesIO(data), filename="extra-synthetic.txt", mime="text/plain"
        )
        drafts = DraftEditService(session, actor)
        version = drafts.import_draft(
            DraftDocument(
                "SYNTHETIC-047-EXTRA", "SYNTHETIC ONLY extra", "other", "public"
            ),
            1,
            DraftFile(
                accepted.file_key,
                accepted.mime,
                accepted.size_bytes,
                accepted.checksum_sha256,
            ),
        )
        drafts.reconcile_sections(
            version.id,
            [
                KeyedSection(
                    "extra",
                    SectionContent(
                        "SYNTHETIC ONLY extra", data.decode(), None, None, True
                    ),
                )
            ],
        )
        lifecycle = LifecycleService(session, actor, prepared.pipeline)
        publish_synthetic(lifecycle, version, directory, public=True)
        document = session.get(Document, version.document_id)
        section = session.scalar(
            select(DocumentSection).where(
                DocumentSection.document_version_id == version.id
            )
        )
        assert document is not None and section is not None
        return KnowledgeHit(
            source=source_ref(document, version, section), excerpt=section.text[:600]
        )


def test_external_unmapped_public_uses_snapshot_and_actual_fts_rank(
    prepared: Prepared, public_competitor: KnowledgeHit
) -> None:
    with prepared.database.unit_of_work() as session:
        session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        )
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        reader = KnowledgeReadService(session, PUBLIC_VIEWER)
        # Real PostgreSQL FTS, using an extra public document outside the mapping.
        page = reader.search(KnowledgeQuery(q="постороннеметка OR индикатор"))
        assert public_competitor in page.items
        expected = prepared.mapping["indicator"].hit
        assert expected in page.items
        actual_rank = page.items.index(expected) + 1
        # Use actual persisted bindings, keeping other outcomes valid.
        observations = [
            Observation(
                q.query_id,
                KnowledgePage(
                    items=[
                        prepared.mapping[k].hit
                        for k in (
                            (
                                q.expected_sections
                                if q.group == "relevant"
                                else q.safe_sections
                            )[:1]
                        )
                    ],
                    total=int(bool(q.expected_sections or q.safe_sections)),
                    limit=5,
                    offset=0,
                ),
            )
            for q in prepared.corpus.queries
        ]
        observations = [
            replace(o, reply=measurement_reply(o.page))
            for o in observations
            if o.page is not None
        ]
        observations[0] = Observation("R01", page, measurement_reply(page))
        result = measure(
            prepared.corpus, prepared.mapping, observations, session=session
        )
        assert result["technical_result"] == "PASS" and result["acl_incidents"] == 0
        rows: Any = result["queries"]
        assert rows[0]["first_relevant_position"] == actual_rank
        assert result["mrr_at_5"] == (19 + 1 / actual_rank) / 20


@pytest.mark.parametrize(
    "placement", ["before", "after", "instead", "no_match", "safe_quote"]
)
def test_unmapped_public_keeps_rank_relevance_and_negative_safety(
    prepared: Prepared, public_competitor: KnowledgeHit, placement: str
) -> None:
    expected = prepared.mapping["indicator"].hit
    items = {
        "before": [public_competitor, expected],
        "after": [expected, public_competitor],
        "instead": [public_competitor],
        "no_match": [public_competitor],
        "safe_quote": [prepared.mapping["safety"].hit, public_competitor],
    }[placement]
    index = 20 if placement == "no_match" else 39 if placement == "safe_quote" else 0
    q = prepared.corpus.queries[index]
    page = KnowledgePage(items=items, total=len(items), limit=5, offset=0)
    with prepared.database.unit_of_work() as session:
        result = measure(
            prepared.corpus,
            prepared.mapping,
            [Observation(q.query_id, page, measurement_reply(page))],
            session=session,
        )
    rows: Any = result["queries"]
    row = rows[index]
    assert row["execution_valid"] and result["acl_incidents"] == 0
    assert (
        len(row["returned_sections"]) == len(items) and None in row["returned_sections"]
    )
    if placement in {"before", "after", "instead"}:
        rank = {"before": 2, "after": 1, "instead": None}[placement]
        assert row["first_relevant_position"] == rank
        assert row["hit_at_5"] == int(rank is not None)
        assert result["mrr_at_5"] == (1 / rank / 20 if rank else 0)
        assert row["safe"]
    else:
        assert not row["safe"]


@pytest.mark.parametrize(
    "state", ["internal", "document_revoked", "draft", "retired", "revoked", "excluded"]
)
def test_unmapped_forbidden_sources_count_page_and_reply_separately(
    prepared: Prepared, public_competitor: KnowledgeHit, state: str, tmp_path: Path
) -> None:
    with (
        prepared.database.unit_of_work() as session,
        session.begin_nested() as transaction,
    ):
        actor = authenticate_operator(session, prepared.admin_login, PASSWORD)
        assert actor is not None
        original = session.get(
            DocumentVersion, public_competitor.source.document_version_id
        )
        assert original is not None
        drafts = DraftEditService(session, actor)
        version = drafts.import_draft(
            DraftDocument(
                "SYNTHETIC-047-" + uuid4().hex,
                "SYNTHETIC ONLY forbidden",
                "other",
                "internal" if state == "internal" else "public",
            ),
            1,
            DraftFile(
                original.file_key,
                original.mime,
                original.size_bytes,
                original.checksum_sha256,
            ),
        )
        drafts.reconcile_sections(
            version.id,
            [
                KeyedSection(
                    "extra",
                    SectionContent(
                        "SYNTHETIC ONLY forbidden",
                        public_competitor.excerpt,
                        None,
                        None,
                        state != "excluded",
                    ),
                )
            ],
        )
        lifecycle = LifecycleService(session, actor, prepared.pipeline)
        if state != "draft":
            publish_synthetic(lifecycle, version, tmp_path, public=state != "internal")
            if state in {"retired", "revoked", "document_revoked"}:
                assert lifecycle.execute(
                    "retire" if state == "retired" else "revoke",
                    version.document_id,
                    version_id=version.id if state != "document_revoked" else None,
                ).ok
        document = session.get(Document, version.document_id)
        section = session.scalar(
            select(DocumentSection).where(
                DocumentSection.document_version_id == version.id
            )
        )
        assert document is not None and section is not None
        source = source_ref(document, version, section)
        forbidden_hit = KnowledgeHit(source=source, excerpt=section.text[:600])
        assert (
            verify_public_source(session, source, forbidden_hit.excerpt) == "forbidden"
        )
        actual = KnowledgeReadService(session, PUBLIC_VIEWER).search(
            KnowledgeQuery(q="постороннеметка")
        )
        assert source.section_id not in [hit.source.section_id for hit in actual.items]
        page = KnowledgePage(items=[forbidden_hit], total=1, limit=5, offset=0)
        # A faulty adapter returns the forbidden source on one or both channels.
        for channel in ("page", "reply", "both"):
            empty_page = KnowledgePage(items=[], total=0, limit=5, offset=0)
            observation = Observation(
                "R01",
                page if channel != "reply" else empty_page,
                measurement_reply(page if channel != "page" else empty_page),
            )
            result = measure(
                prepared.corpus, prepared.mapping, [observation], session=session
            )
            assert result["page_acl_incidents"] == int(channel != "reply")
            assert result["reply_acl_incidents"] == int(channel != "page")
            assert result["acl_incidents"] == (2 if channel == "both" else 1)
            rows: Any = result["queries"]
            assert not rows[0]["execution_valid"]
        transaction.rollback()


@pytest.mark.parametrize(
    "fault",
    [
        "section_id",
        "document_id",
        "document_version_id",
        "title",
        "section",
        "url",
        "page_from",
        "excerpt",
        "reply_source",
    ],
)
def test_unmapped_source_binding_and_missing_evidence_fail_closed(
    prepared: Prepared, public_competitor: KnowledgeHit, fault: str
) -> None:
    hit = public_competitor
    if fault == "excerpt":
        hit = hit.model_copy(update={"excerpt": "SYNTHETIC ONLY forged"})
    elif fault != "reply_source":
        value: Any = (
            uuid4()
            if fault.endswith("_id")
            else 1
            if fault == "page_from"
            else "forged"
        )
        hit = hit.model_copy(
            update={"source": hit.source.model_copy(update={fault: value})}
        )
    page = KnowledgePage(items=[hit], total=1, limit=5, offset=0)
    response = measurement_reply(page)
    if fault == "reply_source":
        response = response.model_copy(
            update={
                "sources": [
                    public_competitor.source.model_copy(update={"document_id": uuid4()})
                ]
            }
        )
    with prepared.database.unit_of_work() as session:
        result = measure(
            prepared.corpus,
            prepared.mapping,
            [Observation("R01", page, response)],
            session=session,
        )
    rows: Any = result["queries"]
    assert not rows[0]["execution_valid"] and result["technical_result"] == "FAIL"
    # Missing rows and fabricated bindings are failures, not evidence of a
    # confirmed disclosure. Confirmed ACL violations have their own test above.
    assert result["acl_incidents"] == 0


@pytest.mark.parametrize(
    "key,loss",
    [("indicator", "visibility"), ("safety", "version"), ("transport", "document")],
)
def test_measured_source_loss_revalidates_file_history_and_replay(
    prepared: Prepared, browser: TestClient, key: str, loss: str
) -> None:
    binding = prepared.mapping[key]
    query = next(q for q in prepared.corpus.queries if key in q.expected_sections)
    reply, owner, request = chat(browser, query.text)
    assert binding.hit.source in reply.sources
    assert browser.get(binding.hit.source.url).status_code == 200
    with prepared.database.unit_of_work() as session:
        actor = authenticate_operator(session, prepared.admin_login, PASSWORD)
        assert actor is not None
        lifecycle = LifecycleService(session, actor, prepared.pipeline)
        assert lifecycle.execute(
            "visibility" if loss == "visibility" else "revoke",
            binding.hit.source.document_id,
            version_id=binding.hit.source.document_version_id
            if loss == "version"
            else None,
            visibility="internal" if loss == "visibility" else None,
        ).ok
    with prepared.database.unit_of_work() as session:
        reader = KnowledgeReadService(session, PUBLIC_VIEWER)
        assert binding.hit.source.section_id not in [
            h.source.section_id
            for h in reader.search(KnowledgeQuery(q=query.text)).items
        ]
        with pytest.raises(SourceNotFound):
            reader.get_source(binding.hit.source.section_id)
    assert browser.get(binding.hit.source.url).status_code == 404
    headers = {"X-Chat-Token": owner["session_token"]}
    replay = browser.post("/api/v1/chat/messages", json=request, headers=headers)
    history = browser.get(
        "/api/v1/chat/sessions/" + owner["session_id"], headers=headers
    )
    assert replay.status_code == history.status_code == 200
    for response in (replay, history):
        assert response.json()["reply"]["resolution"] == "handoff"
        assert response.json()["reply"]["sources"] == []
        assert str(binding.hit.source.section_id) not in response.text
        assert binding.hit.excerpt not in response.text


@pytest.mark.parametrize("query", ["", " ", "\t\n"])
def test_empty_api_and_chat_contract(
    prepared: Prepared, browser: TestClient, query: str
) -> None:
    response = browser.get("/api/v1/knowledge/search", params={"q": query})
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 5, "offset": 0}
    owner = browser.post("/api/v1/chat/sessions", json={"mode": "knowledge"}).json()
    response = browser.post(
        "/api/v1/chat/messages",
        headers={"X-Chat-Token": owner["session_token"]},
        json={
            "session_id": owner["session_id"],
            "request_id": str(uuid4()),
            "expected_version": 0,
            "input": {"type": "text", "value": query},
        },
    )
    assert response.status_code == 422


def test_actual_equal_rank_uuid_order_and_http_pagination(
    prepared: Prepared, browser: TestClient
) -> None:
    expected = sorted(
        str(prepared.mapping[f"tie{i}"].hit.source.section_id) for i in range(6)
    )
    for _ in range(3):
        page = browser.get(
            "/api/v1/knowledge/search", params={"q": "учебный указатель"}
        ).json()
        assert page["total"] == 6
        assert [h["source"]["section_id"] for h in page["items"]] == expected[:5]
    for offset in range(7):
        page = browser.get(
            "/api/v1/knowledge/search",
            params={"q": "учебный указатель", "limit": 1, "offset": offset},
        ).json()
        assert page["total"] == 6
        assert [h["source"]["section_id"] for h in page["items"]] == expected[
            offset : offset + 1
        ]


def test_postgresql_failure_is_not_a_successful_empty_result(
    prepared: Prepared, browser: TestClient
) -> None:
    from sqlalchemy import event
    from sqlalchemy.engine import Connection
    from sqlalchemy.exc import DBAPIError

    def fault(
        connection: Connection,
        _cursor: object,
        statement: str,
        _params: object,
        _context: object,
        _many: bool,
    ) -> None:
        if "ts_rank_cd" in statement:
            connection.exec_driver_sql("SELECT 1/0")

    event.listen(prepared.database.engine, "before_cursor_execute", fault)
    try:
        failed = measure(
            prepared.corpus, prepared.mapping, collect(prepared, browser, [])
        )
        assert failed["db_error_count"] == len(prepared.corpus.queries)
        assert failed["total_relevant"] == 20
        assert failed["negative_unsafe_count"] == 20
        assert failed["technical_result"] == "FAIL" and not failed["complete"]
        with pytest.raises(DBAPIError), prepared.database.unit_of_work() as session:
            KnowledgeReadService(session, PUBLIC_VIEWER).search(
                KnowledgeQuery(q="учебный указатель")
            )
        response = browser.get(
            "/api/v1/knowledge/search", params={"q": "учебный указатель"}
        )
        assert response.status_code == 503
        assert "items" not in response.json()
        assert response.json()["error"]["details"] == {}
    finally:
        event.remove(prepared.database.engine, "before_cursor_execute", fault)
