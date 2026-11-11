"""MVP-053: PG16, real staff HTTP, review, file pipeline and dependencies.

Collected by the existing db:core selection. The composition supplies the same
real synthetic ClamAV used by lifecycle acceptance; no service checks are mocked.
Each test rolls back its own outer PG transaction, including immutable fixtures.
"""

import json
import re
import secrets
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import event, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from test_catalog_applicability import Corpus, confirmed, status
from test_kb_import import PASSWORD, admin, inputs
from test_kb_import import pipeline as pipeline
from test_kb_lifecycle import approval, draft, operation, second_draft
from test_tree_lifecycle import prepared, run

from app.core.csrf import SESSION_COOKIE, StaffSecurity
from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff_models import AuditEvent, StaffSession, StaffUser
from app.core.staff_sessions import token_hash
from app.main import app
from app.modules.catalog.models import CatalogItem, CatalogItemModel, EquipmentModel
from app.modules.kb.files import FilePipeline
from app.modules.kb.import_service import run_preparation
from app.modules.kb.models import Document, DocumentSection, DocumentVersion
from app.modules.kb.read_service import file_url
from app.modules.kb.tree_reads import load_tree

pytestmark = pytest.mark.integration
ORIGIN = "https://localhost:8443"


@pytest.fixture
def content_database(databases: tuple[Database, Database]) -> Iterator[Database]:
    runtime = databases[0]
    with runtime.engine.connect() as connection, connection.begin() as transaction:
        # Preserve the actual Database.unit_of_work commit/rollback implementation,
        # while isolating every immutable synthetic record from the core suites.
        scoped = object.__new__(Database)
        scoped.engine = runtime.engine
        scoped.sessions = sessionmaker(
            connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        yield scoped
        transaction.rollback()


@pytest.fixture
def browser(
    content_database: Database, pipeline: FilePipeline, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    with TestClient(
        app, base_url=ORIGIN, client=("192.0.2.53", 50000), follow_redirects=False
    ) as client:
        monkeypatch.setattr(app.state, "database", content_database)
        monkeypatch.setattr(
            app.state,
            "staff_security",
            StaffSecurity(frozenset({ORIGIN}), secrets.token_hex(32)),
        )
        # Trusted test composition changes only storage and signature release.
        monkeypatch.setattr(
            FilePipeline, "from_environment", classmethod(lambda cls: pipeline)
        )
        yield client


def login(client: TestClient, login_name: str) -> str:
    client.cookies.clear()
    token = client.get("/admin/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/admin/auth/login",
        json={"login": login_name, "password": PASSWORD},
        headers={"Origin": ORIGIN, "X-CSRF-Token": token},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def prepare(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    *,
    reviewed: bool = True,
    public_approval: bool = True,
) -> tuple[str, UUID, UUID]:
    with database.unit_of_work() as session:
        user, document, version = draft(
            session, pipeline, tmp_path, visibility="public", title="PRIVATE-TITLE-053"
        )
        if reviewed:
            proof = approval(
                session,
                pipeline,
                tmp_path,
                user,
                document,
                version,
                public=public_approval,
            )
            assert (
                operation(
                    session, user, pipeline, "review", document, version, proof
                ).code
                == "REVIEWED"
            )
        # Do not inherit the CLI's local idle allowance in this rollback fixture.
        session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '15s'"))
    return user, document, version


def preview(
    client: TestClient,
    csrf: str,
    document: UUID,
    version: UUID | None,
    action: str = "publish",
) -> Response:
    return client.post(
        f"/admin/documents/{document}/preview",
        data={
            "csrf_token": csrf,
            "operation": action,
            **({"version_id": str(version)} if version else {}),
        },
        headers={"Origin": ORIGIN},
    )


def confirmation(response: Response, action: str) -> dict[str, str]:
    assert response.status_code == 200, response.text
    fields = dict(re.findall(r'name="([a-z_]+)" value="([^"]*)"', response.text))
    assert set(fields) == {"csrf_token", "preview_digest", "preview_token", "confirm"}
    assert fields["confirm"] == action
    return fields


def action_url(document: UUID, version: UUID | None, action: str) -> str:
    return (
        f"/admin/documents/{document}"
        + (f"/versions/{version}" if version else "")
        + f"/{action}"
    )


def execute(
    client: TestClient,
    csrf: str,
    document: UUID,
    version: UUID | None,
    action: str = "publish",
) -> Response:
    fields = confirmation(preview(client, csrf, document, version, action), action)
    return client.post(
        action_url(document, version, action), data=fields, headers={"Origin": ORIGIN}
    )


def assert_error(response: Response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert "PUBLISHED" not in response.text


def test_content_gets_are_readonly_bounded_and_redacted(
    browser: TestClient,
    content_database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
) -> None:
    user, document, version = prepare(content_database, pipeline, tmp_path)
    csrf = login(browser, user)
    with content_database.unit_of_work() as session:
        stored = session.get(DocumentVersion, version)
        assert stored is not None
        key = stored.file_key
        section = session.scalar(
            select(DocumentSection).where(
                DocumentSection.document_version_id == version
            )
        )
        assert section is not None
        section.text = "PRIVATE-SECTION-CONTENT-053"
        for number in range(2, 30):
            session.add(
                DocumentVersion(
                    document_id=document,
                    version=number,
                    file_key=key,
                    mime=stored.mime,
                    size_bytes=stored.size_bytes,
                    checksum_sha256=stored.checksum_sha256,
                )
            )
        before = list(session.scalars(select(AuditEvent.id)))
    for url in (
        "/admin/documents",
        f"/admin/documents/{document}",
        f"/admin/documents/{document}?offset=25",
    ):
        response = browser.get(url)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert all(
            value not in response.text
            for value in (
                key,
                "PRIVATE-TITLE-053",
                "PRIVATE-SECTION-CONTENT-053",
                str(pipeline.root),
                PASSWORD,
                browser.cookies[SESSION_COOKIE],
            )
        )
    detail = browser.get(f"/admin/documents/{document}").text
    assert "?offset=25" in detail and str(version) not in detail
    assert str(version) in browser.get(f"/admin/documents/{document}?offset=25").text
    with content_database.unit_of_work() as session:
        assert list(session.scalars(select(AuditEvent.id))) == before
    for query in ("offset=1&offset=2", "offset=10001", "offset=-1", "force=true"):
        assert_error(browser.get("/admin/documents?" + query), 422, "INVALID_REQUEST")
    assert csrf


@pytest.mark.parametrize(
    "authority", ["anonymous", "manager", "disabled", "revoked", "expired"]
)
def test_content_routes_require_current_admin(
    browser: TestClient,
    content_database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    authority: str,
) -> None:
    user, document, version = prepare(content_database, pipeline, tmp_path)
    csrf = login(browser, user)
    with content_database.unit_of_work() as session:
        actor = session.scalar(select(StaffUser).where(StaffUser.login == user))
        row = session.scalar(
            select(StaffSession).where(
                StaffSession.token_hash == token_hash(browser.cookies[SESSION_COOKIE])
            )
        )
        assert actor is not None and row is not None
        if authority == "manager":
            actor.role = "manager"
        elif authority == "disabled":
            actor.is_active = False
        elif authority == "revoked":
            row.revoked_at = utc_now()
        elif authority == "expired":
            row.expires_at = utc_now() - timedelta(seconds=1)
    if authority == "anonymous":
        browser.cookies.clear()
    status, code = (
        (403, "FORBIDDEN")
        if authority == "manager"
        else (401, "AUTHENTICATION_REQUIRED")
    )
    for url in ("/admin/documents", f"/admin/documents/{document}"):
        assert_error(browser.get(url), status, code)
    for url in (
        f"/admin/documents/{document}/preview",
        action_url(document, version, "publish"),
        action_url(document, version, "revoke"),
        action_url(document, None, "revoke"),
    ):
        assert_error(
            browser.post(url, data={"csrf_token": csrf}, headers={"Origin": ORIGIN}),
            status,
            code,
        )


@pytest.mark.parametrize(
    "malformed",
    [
        "origin",
        "csrf",
        "duplicate",
        "duplicate_csrf_header",
        "force",
        "query",
        "direct",
        "missing_confirm",
        "tampered",
        "target",
        "method",
    ],
)
def test_content_forms_reject_direct_and_ambiguous_mutations(
    browser: TestClient,
    content_database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    malformed: str,
) -> None:
    user, document, version = prepare(content_database, pipeline, tmp_path)
    csrf = login(browser, user)
    fields = confirmation(preview(browser, csrf, document, version), "publish")
    url = action_url(document, version, "publish")
    headers = [
        ("Origin", ORIGIN),
        ("Content-Type", "application/x-www-form-urlencoded"),
    ]
    if malformed == "origin":
        headers[0] = ("Origin", "https://untrusted.invalid")
    elif malformed == "csrf":
        fields["csrf_token"] = "forged"
    elif malformed == "force":
        fields["force"] = "true"
    elif malformed == "query":
        url += "?skip_review=true"
    elif malformed == "direct":
        fields = {"csrf_token": csrf, "confirm": "publish"}
    elif malformed == "missing_confirm":
        fields.pop("confirm")
    elif malformed == "tampered":
        fields["preview_token"] += "changed"
    elif malformed == "target":
        url = action_url(document, None, "revoke")
    elif malformed == "duplicate_csrf_header":
        headers += [("X-CSRF-Token", csrf), ("X-CSRF-Token", csrf)]
    body = urlencode(fields) + ("&confirm=publish" if malformed == "duplicate" else "")
    response = (
        browser.get(url)
        if malformed == "method"
        else browser.post(url, content=body, headers=headers)
    )
    if malformed == "method":
        assert response.status_code == 405
    else:
        assert response.status_code in (403, 422)
    with content_database.unit_of_work() as session:
        stored = session.get(DocumentVersion, version)
        assert stored is not None and stored.status == "draft"


@pytest.mark.parametrize(
    "refusal",
    [
        "unreviewed",
        "sections",
        "file_metadata",
        "review_invalid",
        "public_approval",
        "scan",
    ],
)
def test_content_publication_refusals_are_audited_and_atomic(
    browser: TestClient,
    content_database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    refusal: str,
) -> None:
    user, document, version = prepare(
        content_database,
        pipeline,
        tmp_path,
        reviewed=refusal != "unreviewed",
        public_approval=refusal != "public_approval",
    )
    csrf = login(browser, user)
    with content_database.unit_of_work() as session:
        stored = session.get(DocumentVersion, version)
        assert stored is not None
        if refusal == "sections":
            section = session.scalar(
                select(DocumentSection).where(
                    DocumentSection.document_version_id == version
                )
            )
            assert section is not None
            section.text += " synthetic change"
        elif refusal == "file_metadata":
            stored.size_bytes += 1
        elif refusal == "review_invalid":
            # ReviewEvidence validates the actual audit record, not these columns.
            stored.approved_at = utc_now()
    if refusal == "scan":
        # Exercise the real scanner's missing-release failure, no mocked result.
        (pipeline.scanner.database / "test.ndb").unlink()
    expected = {
        "unreviewed": "REVIEW_REQUIRED",
        "sections": "REVIEW_STALE",
        "file_metadata": "REVIEW_STALE",
        "review_invalid": "REVIEW_REQUIRED",
        "public_approval": "PUBLIC_APPROVAL_REQUIRED",
        "scan": "OPERATOR_IO_FAILURE",
    }[refusal]
    response = execute(browser, csrf, document, version)
    assert_error(response, 409, expected)
    with content_database.unit_of_work() as session:
        stored = session.get(DocumentVersion, version)
        assert stored is not None and stored.status == "draft"
        audits = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.entity_id == version, AuditEvent.action == "kb.publish"
                )
            )
        )
        assert [row.result for row in audits] == [expected]
        assert audits[0].details == {}


def test_changed_preview_requires_new_confirmation(
    browser: TestClient,
    content_database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
) -> None:
    user, document, version = prepare(content_database, pipeline, tmp_path)
    csrf = login(browser, user)
    fields = confirmation(preview(browser, csrf, document, version), "publish")
    with content_database.unit_of_work() as session:
        section = session.scalar(
            select(DocumentSection).where(
                DocumentSection.document_version_id == version
            )
        )
        assert section is not None
        section.title += " changed"
    response = browser.post(
        action_url(document, version, "publish"),
        data=fields,
        headers={"Origin": ORIGIN},
    )
    assert_error(response, 409, "PREVIEW_CHANGED")
    assert_error(execute(browser, csrf, document, version), 409, "REVIEW_STALE")


def test_content_commit_failure_never_delivers_success(
    browser: TestClient,
    content_database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
) -> None:
    user, document, version = prepare(content_database, pipeline, tmp_path)
    csrf = login(browser, user)
    fields = confirmation(preview(browser, csrf, document, version), "publish")

    def fail_commit(session: Session) -> None:
        if not session.in_nested_transaction():
            raise SQLAlchemyError("PRIVATE-SQL-ERROR-053")

    event.listen(content_database.sessions, "before_commit", fail_commit)
    try:
        response = browser.post(
            action_url(document, version, "publish"),
            data=fields,
            headers={"Origin": ORIGIN},
        )
    finally:
        event.remove(content_database.sessions, "before_commit", fail_commit)
    assert_error(response, 503, "AUTH_UNAVAILABLE")
    assert "PRIVATE-SQL-ERROR-053" not in response.text
    with content_database.unit_of_work() as session:
        stored = session.get(DocumentVersion, version)
        assert stored is not None and stored.status == "draft"
        assert not session.scalar(
            select(AuditEvent.id).where(
                AuditEvent.action == "kb.publish",
                AuditEvent.entity_id == version,
                AuditEvent.result == "success",
            )
        )


def test_operator_workflow_through_content_forms(
    browser: TestClient,
    content_database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
) -> None:
    # Repeat MVP-043's import/check/review sequence with actual CLI services,
    # then perform publication/replacement/revocation through protected HTTP.
    source, manifest = inputs(tmp_path)
    with content_database.unit_of_work() as session:
        user = admin(session).login
        for check, code in (
            (False, "IMPORTED_DRAFT"),
            (True, "READY_FOR_MANUAL_REVIEW"),
        ):
            result = run_preparation(
                session,
                login=user,
                password=PASSWORD,
                manifest_path=manifest,
                source_path=source if not check else None,
                pipeline=pipeline,
            )
            assert result.code == code
        # Import report returns technical counts rather than content; select the
        # newly imported synthetic manifest's unique code for its stable IDs.
        code = json.loads(manifest.read_text())["code"]
        stored = session.scalar(
            select(DocumentVersion).join(Document).where(Document.code == code)
        )
        assert stored is not None
        document, first = stored.document_id, stored.id
        proof = approval(session, pipeline, tmp_path, user, document, first)
        assert operation(session, user, pipeline, "review", document, first, proof).ok
        session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '15s'"))
    csrf = login(browser, user)
    published = execute(browser, csrf, document, first)
    assert published.status_code == 200 and ">PUBLISHED<" in published.text
    assert execute(browser, csrf, document, first).status_code == 200  # NO_OP
    # Internal content is visible to current staff, hidden after cookie removal.
    url = file_url(document, first)
    assert browser.get(url).status_code == 200
    cookie = browser.cookies[SESSION_COOKIE]
    browser.cookies.clear()
    assert browser.get(url).status_code == 404
    browser.cookies.set(SESSION_COOKIE, cookie)
    with content_database.unit_of_work() as session:
        second = second_draft(session, document, first)
        proof = approval(session, pipeline, tmp_path, user, document, second)
        assert operation(session, user, pipeline, "review", document, second, proof).ok
        session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '15s'"))
    assert execute(browser, csrf, document, second).status_code == 200
    with content_database.unit_of_work() as session:
        assert session.get(DocumentVersion, first).status == "retired"  # type: ignore[union-attr]
        assert session.get(DocumentVersion, second).status == "published"  # type: ignore[union-attr]
    assert browser.get(url).status_code == 200  # permitted exact retired link
    for version in (second, None):
        fields = confirmation(
            preview(browser, csrf, document, version, "revoke"), "revoke"
        )
        for expected in ("REVOKED", "NO_OP"):
            response = browser.post(
                action_url(document, version, "revoke"),
                data=fields,
                headers={"Origin": ORIGIN},
            )
            assert response.status_code == 200 and f">{expected}<" in response.text
        assert browser.get(file_url(document, second)).status_code == 404
    assert browser.get(url).status_code == 404
    with content_database.unit_of_work() as session:
        audits = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action.in_(("kb.publish", "kb.revoke", "kb.retire"))
                )
            )
        )
        actor = session.scalar(select(StaffUser.id).where(StaffUser.login == user))
        assert audits and all(row.actor_id == actor for row in audits)
        assert all(PASSWORD not in str(row.details) for row in audits)


@pytest.mark.parametrize("target", ["version", "document", "replacement"])
def test_content_dependency_preview_invalidation_and_public_reads(
    browser: TestClient,
    content_database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    target: str,
) -> None:
    user, document, first = prepare(content_database, pipeline, tmp_path)
    csrf = login(browser, user)
    assert execute(browser, csrf, document, first).status_code == 200
    with content_database.unit_of_work() as session:
        actor = session.scalar(select(StaffUser.id).where(StaffUser.login == user))
        section = session.scalar(
            select(DocumentSection.id).where(
                DocumentSection.document_version_id == first
            )
        )
        assert actor is not None and section is not None
        model = EquipmentModel(
            code="SYNTHETIC-053-" + uuid4().hex,
            name="SYNTHETIC ONLY",
            description="PRIVATE-MODEL-053",
            status="published",
            manual_document_id=document,
        )
        item = CatalogItem(
            sku="SYNTHETIC-053-" + uuid4().hex,
            kind="product",
            name="SYNTHETIC ONLY",
            description="PRIVATE-ITEM-053",
            unit="test",
            price_mode="on_request",
            price=None,
            tax_note="SYNTHETIC",
            status="published",
        )
        session.add_all([model, item])
        session.flush()
        corpus = Corpus(user, actor, item.id, model.id, section, first, document)
        confirmed(session, corpus, tmp_path)
        _, tree, path, proof = prepared(session, tmp_path, c=corpus)
        assert run(session, corpus, tree, "publish", path, proof).ok
        assert status(session, corpus) == "confirmed"
        assert load_tree(session, tree_id=tree) is not None
        revision = item.revision
        second = second_draft(session, document, first)
        proof = approval(
            session, pipeline, tmp_path, user, document, second, public=True
        )
        assert operation(session, user, pipeline, "review", document, second, proof).ok
        session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '15s'"))
    selected = (
        second if target == "replacement" else first if target == "version" else None
    )
    action = "publish" if target == "replacement" else "revoke"
    report = preview(browser, csrf, document, selected, action)
    fields = confirmation(report, action)
    for identifier in (corpus.model, corpus.item, tree):
        assert str(identifier) in report.text
    assert all(
        value not in report.text
        for value in (
            "PRIVATE-MODEL-053",
            "PRIVATE-ITEM-053",
            str(pipeline.root),
            "Synthetic text",
            "graph",
            "file_key",
        )
    )
    url = action_url(document, selected, action)
    result = browser.post(url, data=fields, headers={"Origin": ORIGIN})
    assert result.status_code == 200
    with content_database.unit_of_work() as session:
        link = session.get(CatalogItemModel, (corpus.item, corpus.model))
        assert link is not None and not link.public_allowed
        assert link.published_by is None and link.published_at is None
        assert link.approved_at is None
        assert session.get(CatalogItem, corpus.item).revision == revision + 1  # type: ignore[union-attr]
        assert status(session, corpus) == "unknown"
        assert session.get(EquipmentModel, corpus.model).manual_document_id == document  # type: ignore[union-attr]
        if target != "replacement":
            assert load_tree(session, tree_id=tree) is None
    # A repeated identical confirmed request keeps the original NO_OP semantics.
    result = browser.post(url, data=fields, headers={"Origin": ORIGIN})
    assert result.status_code == 200 and ">NO_OP<" in result.text
    cookie = browser.cookies[SESSION_COOKIE]
    browser.cookies.clear()
    assert browser.get(file_url(document, first)).status_code == (
        200 if target == "replacement" else 404
    )
    browser.cookies.set(SESSION_COOKIE, cookie)
    if target == "version":
        # A later publication of the next approved edition cannot restore the
        # revoked link's separate disclosure permission or dead tree source.
        assert execute(browser, csrf, document, second).status_code == 200
        with content_database.unit_of_work() as session:
            assert status(session, corpus) == "unknown"
            assert not session.get(
                CatalogItemModel, (corpus.item, corpus.model)
            ).public_allowed  # type: ignore[union-attr]
            assert load_tree(session, tree_id=tree) is None


def test_content_cli_ui_audit_business_parity(
    browser: TestClient,
    content_database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
) -> None:
    cli_user, cli_document, cli_version = prepare(content_database, pipeline, tmp_path)
    ui_user, ui_document, ui_version = prepare(content_database, pipeline, tmp_path)
    with content_database.unit_of_work() as session:
        for action in ("publish", "publish", "revoke", "revoke"):
            assert operation(
                session, cli_user, pipeline, action, cli_document, cli_version
            ).ok
        session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '15s'"))
    csrf = login(browser, ui_user)
    for action in ("publish", "publish", "revoke", "revoke"):
        assert (
            execute(browser, csrf, ui_document, ui_version, action).status_code == 200
        )
    with content_database.unit_of_work() as session:

        def business_events(version: UUID, user: str) -> list[tuple[object, ...]]:
            actor = session.scalar(select(StaffUser.id).where(StaffUser.login == user))
            events = session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.entity_id == version,
                    AuditEvent.action.in_(("kb.publish", "kb.revoke")),
                )
                .order_by(AuditEvent.at, AuditEvent.id)
            ).all()
            assert events and all(row.actor_id == actor for row in events)
            return [
                (
                    row.action,
                    row.entity_type,
                    row.result,
                    {
                        key: value
                        for key, value in row.details.items()
                        if key not in {"document_id", "review_event_id"}
                    },
                )
                for row in events
            ]

        assert business_events(cli_version, cli_user) == business_events(
            ui_version, ui_user
        )
