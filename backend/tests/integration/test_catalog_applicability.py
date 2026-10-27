"""MVP-024: synthetic PostgreSQL 16 corpus, public HTTP and actual row locks."""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from test_kb_import import PASSWORD, admin
from test_kb_import import pipeline as pipeline
from test_kb_import import session as session
from test_kb_lifecycle import (
    approval,
    cli_request,
    draft,
    live_http,
    operation,
    review_publish,
    second_draft,
)

from app.core.csrf import SESSION_COOKIE
from app.core.db import Database
from app.core.staff import authenticate_operator, disable_staff
from app.core.staff_models import AuditEvent, StaffSession
from app.core.staff_sessions import StaffIdentity, token_hash
from app.main import app
from app.modules.catalog.applicability_write import (
    ApplicabilityWriteService,
    Operation,
    run_applicability,
)
from app.modules.catalog.models import CatalogItem, CatalogItemModel, EquipmentModel
from app.modules.catalog.repository import CatalogRepository
from app.modules.catalog.router import catalog_session
from app.modules.catalog.service import CatalogNotFound, CatalogService
from app.modules.catalog.write_dto import ModelState
from app.modules.catalog.write_service import CatalogWriteService
from app.modules.kb.files import FilePipeline
from app.modules.kb.models import Document, DocumentSection, DocumentVersion
from app.modules.kb.read_service import source_ref
from app.modules.orders.models import ConsentText
from app.modules.orders.security import OrderSecurity
from app.modules.orders.service import OrderRejected, create_order, replay_order
from app.schemas.catalog import CatalogQuery
from app.schemas.common import OrderHeaders
from app.schemas.orders import OrderCreate

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Corpus:
    login: str
    actor: UUID
    item: UUID
    model: UUID
    section: UUID
    version: UUID
    document: UUID


def corpus(
    session: Session,
    *,
    kind: str = "product",
    visibility: str = "public",
    allowed: bool = True,
    sku: str | None = None,
) -> Corpus:
    user = admin(session)
    item = CatalogItem(
        sku=sku or "SYNTHETIC-024-" + uuid4().hex,
        kind=kind,
        name="SYNTHETIC 024 search",
        description="SYNTHETIC ONLY",
        unit="test",
        price_mode="on_request",
        price=None,
        tax_note="SYNTHETIC",
        status="published",
        attributes={"compatibility": "never evidence"},
    )
    model = EquipmentModel(
        code="SYNTHETIC-024-" + uuid4().hex,
        name="SYNTHETIC",
        description="SYNTHETIC",
        status="published",
    )
    doc = Document(
        code="SYNTHETIC-024-" + uuid4().hex,
        title="INTERNAL-SYNTHETIC-TITLE"
        if visibility == "internal"
        else "SYNTHETIC PUBLIC",
        kind="other",
        visibility=visibility,
    )
    session.add_all([item, model, doc])
    session.flush()
    version = DocumentVersion(
        document_id=doc.id,
        version=1,
        file_key="Synthetic024Key",
        mime="text/plain",
        size_bytes=20,
        checksum_sha256="a" * 64,
    )
    session.add(version)
    session.flush()
    section = DocumentSection(
        document_version_id=version.id,
        section_key="test",
        title="SYNTHETIC SECTION",
        text="INTERNAL-SYNTHETIC-TEXT",
        assistant_allowed=allowed,
    )
    session.add(section)
    session.flush()
    version.status = "published"
    version.reviewed_by = user.id
    version.approved_at = version.published_at = datetime.now(UTC)
    session.flush()
    return Corpus(
        user.login, user.id, item.id, model.id, section.id, version.id, doc.id
    )


def command(
    session: Session,
    c: Corpus,
    action: Operation,
    *,
    section: UUID | None = None,
    proof: Path | None = None,
    login: str | None = None,
) -> str:
    return run_applicability(
        session,
        login=login or c.login,
        password=PASSWORD,
        operation=action,
        item_id=c.item,
        model_id=c.model,
        section_id=section,
        evidence_path=proof,
    ).code


def private_json(tmp_path: Path, values: dict[str, object]) -> Path:
    path = tmp_path / (uuid4().hex + ".json")
    path.write_text(json.dumps(values))
    path.chmod(0o600)
    return path


def proofs(session: Session, c: Corpus, tmp_path: Path) -> tuple[Path, Path]:
    assert command(session, c, "candidate", section=c.section) in {"CANDIDATE", "NO_OP"}
    values: dict[str, object] = {
        "item_id": str(c.item),
        "model_id": str(c.model),
        "evidence_section_id": str(c.section),
        "expert_name": "SYNTHETIC TEST Expert",
        "review_reference": "SYNTHETIC:APPLICABILITY:024",
        "approved_at": datetime.now(UTC).isoformat(),
        "decision": "approved",
        "applicability_checked": True,
    }
    review = private_json(tmp_path, values)
    result = run_applicability(
        session,
        login=c.login,
        password=PASSWORD,
        operation="review",
        item_id=c.item,
        model_id=c.model,
        evidence_path=review,
    )
    assert result.code == "REVIEWED", result
    release = private_json(
        tmp_path,
        {
            **values,
            "review_event_id": result.technical["review_event_id"],
            "fact_publication_allowed": True,
            "publication_reference": "SYNTHETIC:PUBLIC:024",
        },
    )
    return review, release


def confirmed(session: Session, c: Corpus, tmp_path: Path) -> tuple[Path, Path]:
    review, release = proofs(session, c, tmp_path)
    assert command(session, c, "publish", proof=release) == "PUBLISHED"
    return review, release


def service(session: Session) -> CatalogService:
    return CatalogService(CatalogRepository(session))


def status(session: Session, c: Corpus) -> str:
    applicability = service(session).item(c.item, c.model).applicability
    assert applicability is not None
    return applicability.status


@pytest.mark.parametrize("kind", ["product", "service"])
@pytest.mark.parametrize(
    "visibility,allowed,source_count",
    [
        ("public", True, 1),
        ("public", False, 0),
        ("internal", True, 0),
    ],
)
def test_public_fact_sources_and_cookie_boundary(
    session: Session,
    tmp_path: Path,
    kind: str,
    visibility: str,
    allowed: bool,
    source_count: int,
) -> None:
    c = corpus(session, kind=kind, visibility=visibility, allowed=allowed)
    confirmed(session, c, tmp_path)
    assert service(session).item(c.item, None).applicability is None
    card = service(session).item(c.item, c.model)
    assert card.applicability and card.applicability.status == "confirmed"
    assert len(card.applicability.sources) == source_count
    if source_count:
        document = session.get(Document, c.document)
        version = session.get(DocumentVersion, c.version)
        section = session.get(DocumentSection, c.section)
        assert document and version and section
        assert card.applicability.sources == [source_ref(document, version, section)]
    session.add(
        StaffSession(
            user_id=c.actor,
            token_hash=token_hash("a" * 64),
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
    )
    session.flush()
    app.dependency_overrides[catalog_session] = lambda: session
    try:
        with TestClient(app) as client:
            path = f"/api/v1/catalog/items/{c.item}?model_id={c.model}"
            anonymous = client.get(path)
            client.cookies.set(SESSION_COOKIE, "a" * 64)
            staff_cookie = client.get(path)
            assert staff_cookie.json() == anonymous.json()
            listing = client.get(
                "/api/v1/catalog/items", params={"model_id": str(c.model)}
            ).json()
            assert listing["total"] == 1
            assert (
                listing["items"][0]["applicability"]
                == anonymous.json()["applicability"]
            )
            if not source_count:
                for secret in (
                    str(c.section),
                    str(c.version),
                    str(c.document),
                    "INTERNAL-SYNTHETIC-TITLE",
                    "INTERNAL-SYNTHETIC-TEXT",
                    "Synthetic024Key",
                ):
                    assert secret not in anonymous.text and secret not in json.dumps(
                        listing
                    )
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "change",
    [
        "no_link",
        "candidate",
        "review_only",
        "draft_version",
        "retired",
        "revoked_version",
        "revoked_document",
        "draft_item",
        "archived_item",
        "draft_model",
        "archived_model",
    ],
)
def test_missing_condition_never_confirms(
    session: Session, tmp_path: Path, change: str
) -> None:
    c = corpus(session)
    if change != "no_link":
        assert command(session, c, "candidate", section=c.section) == "CANDIDATE"
    if change not in {"no_link", "candidate"}:
        _, release = proofs(session, c, tmp_path)
        if change != "review_only":
            assert command(session, c, "publish", proof=release) == "PUBLISHED"
    if change in {"retired", "revoked_version"}:
        session.execute(
            text("UPDATE app.document_versions SET status=:status WHERE id=:id"),
            {
                "status": "retired" if change == "retired" else "revoked",
                "id": c.version,
            },
        )
        # Deliberately skip lifecycle invalidation: read-time check must still reject.
        assert session.get(CatalogItemModel, (c.item, c.model)).public_allowed  # type: ignore[union-attr]
    elif change == "revoked_document":
        session.execute(
            text("UPDATE app.documents SET status='revoked' WHERE id=:id"),
            {"id": c.document},
        )
    elif change == "draft_version":
        version = second_draft(session, c.document, c.version)
        section = session.scalar(
            select(DocumentSection.id).where(
                DocumentSection.document_version_id == version
            )
        )
        session.execute(
            text(
                "UPDATE app.catalog_item_models SET evidence_section_id=:part "
                "WHERE item_id=:item"
            ),
            {"part": section, "item": c.item},
        )
    elif change.endswith("_item"):
        session.execute(
            text("UPDATE app.catalog_items SET status=:status WHERE id=:id"),
            {"status": change.split("_")[0], "id": c.item},
        )
    elif change.endswith("_model"):
        session.execute(
            text("UPDATE app.equipment_models SET status=:status WHERE id=:id"),
            {"status": change.split("_")[0], "id": c.model},
        )
    page = service(session).items(CatalogQuery(model_id=c.model))
    assert page.total == 0 and page.items == []
    if change.endswith("_item") or change.endswith("_model"):
        with pytest.raises(CatalogNotFound):
            service(session).item(c.item, c.model)
    else:
        assert status(session, c) == "unknown"
        assert service(session).item(c.item, c.model).applicability.sources == []  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "field,value",
    [
        ("expert_name", None),
        ("expert_name", "  "),
        ("expert_name", "test\n"),
        ("review_reference", None),
        ("review_reference", "/private/path"),
        ("approved_at", None),
        ("published_by", None),
        ("published_at", None),
    ],
)
def test_postgresql_publication_check(
    session: Session, tmp_path: Path, field: str, value: object
) -> None:
    c = corpus(session)
    confirmed(session, c, tmp_path)
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                text(
                    f"UPDATE app.catalog_item_models SET {field}=:value "
                    "WHERE item_id=:item"
                ),
                {"value": value, "item": c.item},
            )
    assert status(session, c) == "confirmed"


def test_fk_pk_indexes_and_runtime_ddl(session: Session, tmp_path: Path) -> None:
    c = corpus(session)
    confirmed(session, c, tmp_path)
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(
                CatalogItemModel(
                    item_id=c.item, model_id=c.model, evidence_section_id=c.section
                )
            )
            session.flush()
    for table, identifier in (
        ("catalog_items", c.item),
        ("equipment_models", c.model),
        ("staff_users", c.actor),
        ("document_sections", c.section),
    ):
        with pytest.raises(DBAPIError):
            with session.begin_nested():
                session.execute(
                    text(f"DELETE FROM app.{table} WHERE id=:id"), {"id": identifier}
                )
    for statement in (
        "ALTER TABLE app.catalog_item_models DISABLE TRIGGER ALL",
        "CREATE TABLE app.forbidden024 (id integer)",
        "SET LOCAL session_replication_role = replica",
    ):
        with pytest.raises(DBAPIError):
            with session.begin_nested():
                session.execute(text(statement))
    indexes = set(
        session.scalars(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname='app' "
                "AND tablename='catalog_item_models'"
            )
        )
    )
    assert indexes == {
        "pk_catalog_item_models",
        "ix_catalog_item_models_model_item",
        "ix_catalog_item_models_evidence_section_id",
    }


def test_filters_count_page_and_unknown_model(session: Session, tmp_path: Path) -> None:
    c = corpus(session, sku="SYNTHETIC024-01")
    confirmed(session, c, tmp_path)
    for index, kind, publish in (
        (0, "product", False),
        (2, "service", True),
        (3, "product", True),
        (4, "service", False),
    ):
        other = corpus(session, kind=kind, sku=f"SYNTHETIC024-{index:02}")
        other = Corpus(
            other.login,
            other.actor,
            other.item,
            c.model,
            other.section,
            other.version,
            other.document,
        )
        if publish:
            confirmed(session, other, tmp_path)
        else:
            command(session, other, "candidate", section=other.section)
    complete = service(session).items(CatalogQuery(model_id=c.model))
    assert complete.total == 3
    pages = [
        service(session).items(CatalogQuery(model_id=c.model, limit=1, offset=i))
        for i in range(5)
    ]
    assert all(p.total == 3 for p in pages)
    assert [p.items[0].id for p in pages[:3]] == [r.id for r in complete.items]
    assert pages[3].items == pages[4].items == []
    filtered = service(session).items(
        CatalogQuery(model_id=c.model, q="search", kind="service")
    )
    assert filtered.total == 1 and filtered.items[0].sku == "SYNTHETIC024-02"
    assert service(session).items(CatalogQuery(model_id=uuid4())).total == 0
    assert (
        service(session).items(CatalogQuery(model_id=c.model, q="' OR true --")).total
        == 0
    )


def test_workflow_revision_noop_replace_and_re_review(
    session: Session, tmp_path: Path
) -> None:
    c = corpus(session)
    original = session.get(CatalogItem, c.item)
    assert original is not None
    review, release = confirmed(session, c, tmp_path)
    revision = original.revision
    assert command(session, c, "publish", proof=release) == "NO_OP"
    assert command(session, c, "review", proof=review) == "NO_OP"
    assert original.revision == revision
    assert command(session, c, "revoke") == "REVOKED"
    assert original.revision == revision + 1 and status(session, c) == "unknown"
    assert command(session, c, "revoke") == "NO_OP"
    assert command(session, c, "publish", proof=release) == "REVIEW_REQUIRED"
    assert command(session, c, "review", proof=review) == "REVIEWED"
    assert command(session, c, "publish", proof=release) == "PUBLIC_APPROVAL_STALE"
    _, release = proofs(session, c, tmp_path)
    assert command(session, c, "publish", proof=release) == "PUBLISHED"
    other = corpus(session, visibility="internal")
    revision = original.revision
    assert command(session, c, "replace", section=other.section) == "REPLACED"
    assert original.revision == revision + 1
    link = session.get(CatalogItemModel, (c.item, c.model))
    assert link and not link.public_allowed and link.approved_at is None
    assert link.expert_name is None and link.review_reference is None
    assert link.published_by is None and link.published_at is None
    assert command(session, c, "replace", section=other.section) == "NO_OP"
    assert original.revision == revision + 1
    assert status(session, c) == "unknown"
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == c.item)
    ).all()
    assert all(e.actor_id == c.actor for e in events)
    data = json.dumps([e.details for e in events])
    assert "SYNTHETIC:APPLICABILITY:024" in data and "before_public_allowed" in data
    assert "INTERNAL-SYNTHETIC-TEXT" not in data and PASSWORD not in data


@pytest.mark.parametrize("field", ["expert_name", "review_reference", "approved_at"])
def test_changed_review_clears_permission(
    session: Session, tmp_path: Path, field: str
) -> None:
    c = corpus(session)
    review, _ = confirmed(session, c, tmp_path)
    data = json.loads(review.read_text())
    data[field] = {
        "expert_name": "SYNTHETIC Different Expert",
        "review_reference": "SYNTHETIC:NEW:024",
        "approved_at": "2025-01-01T00:00:00+00:00",
    }[field]
    before = service(session).item(c.item, c.model).revision
    assert (
        command(session, c, "review", proof=private_json(tmp_path, data)) == "REVIEWED"
    )
    assert status(session, c) == "unknown"
    assert service(session).item(c.item, c.model).revision == before + 1


@pytest.mark.parametrize(
    "action", ["candidate", "replace", "review", "publish", "revoke", "preview"]
)
@pytest.mark.parametrize(
    "role,active,expected",
    [("manager", True, "FORBIDDEN"), ("admin", False, "AUTHENTICATION_DENIED")],
)
def test_operator_rbac(
    session: Session, action: Operation, role: str, active: bool, expected: str
) -> None:
    user = admin(session, role=role, active=active)
    c = Corpus(user.login, user.id, uuid4(), uuid4(), uuid4(), uuid4(), uuid4())
    assert (
        command(
            session,
            c,
            action,
            section=c.section if action in {"candidate", "replace"} else None,
        )
        == expected
    )
    denial = session.scalar(select(AuditEvent).where(AuditEvent.entity_id == c.item))
    assert denial and denial.result == expected


@pytest.mark.parametrize("action", ["retire", "version_revoke", "document_revoke"])
def test_lifecycle_atomic_invalidation_and_revision_once(
    session: Session, pipeline: FilePipeline, tmp_path: Path, action: str
) -> None:
    c = corpus(session)
    confirmed(session, c, tmp_path)
    other = corpus(session)
    second = Corpus(
        c.login, c.actor, c.item, other.model, c.section, c.version, c.document
    )
    confirmed(session, second, tmp_path)
    before = service(session).item(c.item, c.model).revision
    preview = operation(session, c.login, pipeline, "preview", c.document)
    deps = preview.technical["dependencies"]
    assert isinstance(deps, list)
    assert deps[1]["count"] == 1 and deps[1]["link_count"] == 2
    assert {p["model_id"] for p in deps[1]["links"]} == {str(c.model), str(other.model)}
    result = operation(
        session,
        c.login,
        pipeline,
        "retire" if action == "retire" else "revoke",
        c.document,
        None if action == "document_revoke" else c.version,
    )
    assert result.ok
    for model in (c.model, other.model):
        link = session.get(CatalogItemModel, (c.item, model), populate_existing=True)
        assert (
            link
            and not link.public_allowed
            and link.published_by is None
            and link.published_at is None
            and link.approved_at is None
        )
    assert status(session, c) == "unknown"
    assert service(session).item(c.item, c.model).revision == before + 1
    operation(
        session,
        c.login,
        pipeline,
        "retire" if action == "retire" else "revoke",
        c.document,
        None if action == "document_revoke" else c.version,
    )
    assert service(session).item(c.item, c.model).revision == before + 1
    if action == "retire":
        session.execute(
            text("UPDATE app.document_versions SET status='published' WHERE id=:id"),
            {"id": c.version},
        )
        assert status(session, c) == "unknown"


def test_visibility_changes_only_source(
    session: Session, pipeline: FilePipeline, tmp_path: Path
) -> None:
    c = corpus(session)
    confirmed(session, c, tmp_path)
    before = service(session).item(c.item, c.model)
    assert before.applicability and before.applicability.sources
    assert operation(
        session, c.login, pipeline, "visibility", c.document, visibility="internal"
    ).ok
    after = service(session).item(c.item, c.model)
    assert (
        after.applicability
        and after.applicability.status == "confirmed"
        and after.applicability.sources == []
    )
    assert after.revision == before.revision
    # Synthetic DB setup simulates the separately authorized opposite transition.
    session.execute(
        text("UPDATE app.documents SET visibility='public' WHERE id=:id"),
        {"id": c.document},
    )
    assert service(session).item(c.item, c.model).applicability == before.applicability


def test_new_edition_retires_and_invalidates(
    session: Session, pipeline: FilePipeline, tmp_path: Path
) -> None:
    login, document, version = draft(session, pipeline, tmp_path)
    review_publish(session, pipeline, tmp_path, login, document, version)
    base = corpus(session)
    part = session.scalar(
        select(DocumentSection.id).where(DocumentSection.document_version_id == version)
    )
    assert part is not None
    c = Corpus(login, base.actor, base.item, base.model, part, version, document)
    confirmed(session, c, tmp_path)
    before = service(session).item(c.item, c.model).revision
    second = second_draft(session, document, version)
    review_publish(session, pipeline, tmp_path, login, document, second)
    assert service(session).item(c.item, c.model).revision == before + 1
    assert status(session, c) == "unknown"
    assert not session.get(CatalogItemModel, (c.item, c.model)).public_allowed  # type: ignore[union-attr]


@pytest.mark.parametrize("which", ["publish", "retire", "revoke", "lifecycle_audit"])
def test_audit_failure_rolls_back_every_change(
    session: Session, pipeline: FilePipeline, tmp_path: Path, which: str
) -> None:
    c = corpus(session)
    _, release = proofs(session, c, tmp_path)
    if which != "publish":
        assert command(session, c, "publish", proof=release) == "PUBLISHED"
    before = service(session).item(c.item, c.model).revision

    def fail(mapper: object, connection: object, target: AuditEvent) -> None:
        expected = {
            "publish": "applicability.publish",
            "retire": "applicability.invalidate",
            "revoke": "applicability.invalidate",
            "lifecycle_audit": "kb.retire",
        }[which]
        if target.action == expected:
            raise SQLAlchemyError("synthetic audit failure")

    event.listen(AuditEvent, "before_insert", fail)
    try:
        with pytest.raises(SQLAlchemyError):
            if which == "publish":
                command(session, c, "publish", proof=release)
            else:
                operation(
                    session,
                    c.login,
                    pipeline,
                    "revoke" if which == "revoke" else "retire",
                    c.document,
                    c.version,
                )
    finally:
        event.remove(AuditEvent, "before_insert", fail)
    assert service(session).item(c.item, c.model).revision == before
    assert session.get(DocumentVersion, c.version).status == "published"  # type: ignore[union-attr]
    assert status(session, c) == ("unknown" if which == "publish" else "confirmed")


def wait_for_lock(database: Database) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with database.engine.connect() as connection:
            if connection.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                    "WHERE cardinality(pg_blocking_pids(pid)) > 0 "
                    "AND datname=current_database())"
                )
            ):
                return
        time.sleep(0.01)
    pytest.fail("Expected an actual PostgreSQL lock waiter")


@pytest.mark.parametrize(
    "contender", ["retire", "revoke", "publish", "replace", "disable"]
)
def test_concurrent_publication_and_operator_changes(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contender: str,
) -> None:
    with database.unit_of_work() as session:
        c = corpus(session)
        _, release = proofs(session, c, tmp_path)
        replacement = corpus(session)
        disabler = admin(session).login
    entered, finish = Event(), Event()
    original = ApplicabilityWriteService._audit

    def pause(
        self: ApplicabilityWriteService,
        action: Operation,
        *args: object,
        **kwargs: object,
    ) -> AuditEvent:
        result = original(self, action, *args, **kwargs)  # type: ignore[arg-type]
        if action == "publish" and not entered.is_set():
            entered.set()
            assert finish.wait(10)
        return result

    monkeypatch.setattr(ApplicabilityWriteService, "_audit", pause)

    def publish() -> str:
        with database.unit_of_work() as session:
            return command(session, c, "publish", proof=release)

    def compete() -> str:
        with database.unit_of_work() as session:
            if contender in {"retire", "revoke"}:
                return operation(
                    session,
                    c.login,
                    pipeline,
                    "retire" if contender == "retire" else "revoke",
                    c.document,
                    c.version,
                ).code
            if contender == "disable":
                return disable_staff(
                    session,
                    admin_login=disabler,
                    admin_password=PASSWORD,
                    user_id=c.actor,
                ).result
            if contender == "replace":
                return command(session, c, "replace", section=replacement.section)
            return command(session, c, "publish", proof=release)

    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(publish)
        assert entered.wait(5)
        competing = pool.submit(compete)
        try:
            wait_for_lock(database)
            assert not competing.done()
        finally:
            finish.set()
        assert publishing.result(timeout=10) == "PUBLISHED"
        assert (
            competing.result(timeout=10)
            == {
                "retire": "RETIRED",
                "revoke": "REVOKED",
                "publish": "NO_OP",
                "replace": "REPLACED",
                "disable": "success",
            }[contender]
        )
    with database.unit_of_work() as session:
        assert status(session, c) == (
            "confirmed" if contender in {"publish", "disable"} else "unknown"
        )
        if contender == "publish":
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.entity_id == c.item,
                        AuditEvent.action == "applicability.publish",
                        AuditEvent.result == "success",
                    )
                )
                == 1
            )
        if contender == "disable":
            assert command(session, c, "revoke") == "AUTHENTICATION_DENIED"


@pytest.mark.parametrize("which", ["publish", "retire"])
def test_commit_failure_rolls_back_applicability_and_lifecycle(
    database: Database, pipeline: FilePipeline, tmp_path: Path, which: str
) -> None:
    with database.unit_of_work() as session:
        c = corpus(session)
        _, release = proofs(session, c, tmp_path)
        if which == "retire":
            command(session, c, "publish", proof=release)
        before = service(session).item(c.item, c.model).revision

    def fail_commit(session: Session) -> None:
        if not session.in_nested_transaction():
            raise SQLAlchemyError("synthetic outer commit failure")

    event.listen(database.sessions, "before_commit", fail_commit)
    try:
        with pytest.raises(SQLAlchemyError):
            with database.unit_of_work() as session:
                if which == "publish":
                    assert command(session, c, "publish", proof=release) == "PUBLISHED"
                else:
                    assert operation(
                        session, c.login, pipeline, "retire", c.document, c.version
                    ).ok
    finally:
        event.remove(database.sessions, "before_commit", fail_commit)
    with database.unit_of_work() as session:
        assert service(session).item(c.item, c.model).revision == before
        assert status(session, c) == ("unknown" if which == "publish" else "confirmed")
        assert session.get(DocumentVersion, c.version).status == "published"  # type: ignore[union-attr]
        assert not session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.entity_id == c.item,
                AuditEvent.action
                == (
                    "applicability.publish"
                    if which == "publish"
                    else "applicability.invalidate"
                ),
                AuditEvent.result == "success",
            )
        )


@pytest.mark.parametrize("change", ["item_status", "retire", "revoke", "replace"])
def test_change_wins_before_publication_recheck(
    database: Database, pipeline: FilePipeline, tmp_path: Path, change: str
) -> None:
    with database.unit_of_work() as session:
        c = corpus(session)
        _, release = proofs(session, c, tmp_path)
        other = corpus(session)
    with database.unit_of_work() as holder:
        if change == "item_status":
            # A real independent item writer; no global staff lock can mask the
            # required PostgreSQL item row lock or stale ORM re-read here.
            holder.execute(
                text(
                    "UPDATE app.catalog_items SET status='archived', "
                    "revision=revision+1 WHERE id=:id"
                ),
                {"id": c.item},
            )
        elif change == "replace":
            assert command(holder, c, "replace", section=other.section) == "REPLACED"
        else:
            assert operation(
                holder,
                c.login,
                pipeline,
                "retire" if change == "retire" else "revoke",
                c.document,
                c.version,
            ).ok
        with ThreadPoolExecutor(max_workers=1) as pool:

            def publish() -> str:
                with database.unit_of_work() as session:
                    return command(session, c, "publish", proof=release)

            pending = pool.submit(publish)
            try:
                wait_for_lock(database)
            finally:
                # Commit the first operation while the contender is waiting.
                holder.commit()
            result = pending.result(timeout=10)
            assert result in {
                "PUBLISHED_REQUIRED",
                "EVIDENCE_UNAVAILABLE",
                "REVIEW_REQUIRED",
            }
    with database.unit_of_work() as session:
        link = session.get(CatalogItemModel, (c.item, c.model))
        assert link and not link.public_allowed


def test_row_lock_timeout_rolls_back_and_can_retry(
    database: Database, tmp_path: Path
) -> None:
    with database.unit_of_work() as session:
        c = corpus(session)
        _, release = proofs(session, c, tmp_path)
    with database.unit_of_work() as holder:
        holder.execute(
            select(Document).where(Document.id == c.document).with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:

            def publish() -> str:
                with database.unit_of_work() as session:
                    return command(session, c, "publish", proof=release)

            pending = pool.submit(publish)
            wait_for_lock(database)
            with pytest.raises(DBAPIError) as error:
                pending.result(timeout=6)
            assert getattr(error.value.orig, "sqlstate", None) == "55P03"
    with database.unit_of_work() as session:
        assert status(session, c) == "unknown"
        assert command(session, c, "publish", proof=release) == "PUBLISHED"


def test_new_edition_races_old_section_permission(
    database: Database, pipeline: FilePipeline, tmp_path: Path
) -> None:
    with database.unit_of_work() as session:
        login, document, version = draft(session, pipeline, tmp_path)
        review_publish(session, pipeline, tmp_path, login, document, version)
        base = corpus(session)
        part = session.scalar(
            select(DocumentSection.id).where(
                DocumentSection.document_version_id == version
            )
        )
        assert part is not None
        c = Corpus(login, base.actor, base.item, base.model, part, version, document)
        _, release = proofs(session, c, tmp_path)
        second = second_draft(session, document, version)
        proof = approval(session, pipeline, tmp_path, login, document, second)
        assert operation(session, login, pipeline, "review", document, second, proof).ok
    # Hold the lifecycle's real document/version locks through its commit.
    with database.unit_of_work() as holder:
        assert operation(holder, login, pipeline, "publish", document, second).ok
        with ThreadPoolExecutor(max_workers=1) as pool:

            def publish() -> str:
                with database.unit_of_work() as session:
                    return command(session, c, "publish", proof=release)

            pending = pool.submit(publish)
            try:
                wait_for_lock(database)
            finally:
                holder.commit()
            assert pending.result(timeout=10) == "EVIDENCE_UNAVAILABLE"
    with database.unit_of_work() as session:
        assert status(session, c) == "unknown"


def test_single_http_snapshot_and_immediate_revoke(
    database: Database, pipeline: FilePipeline, tmp_path: Path
) -> None:
    with database.unit_of_work() as session:
        c = corpus(session)
        confirmed(session, c, tmp_path)
    selected, finish = Event(), Event()

    def pause(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if "allowed_catalog AS MATERIALIZED" in statement and not selected.is_set():
            selected.set()
            assert finish.wait(10)

    event.listen(database.engine, "after_cursor_execute", pause)
    try:
        with (
            live_http(database, pipeline) as browser,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            listing = pool.submit(
                browser.get, "/api/v1/catalog/items", params={"model_id": str(c.model)}
            )
            assert selected.wait(5)
            try:
                with database.unit_of_work() as session:
                    assert operation(
                        session, c.login, pipeline, "revoke", c.document
                    ).ok
            finally:
                finish.set()
            previous = listing.result(timeout=10).json()
            assert (
                previous["total"] == 1
                and previous["items"][0]["applicability"]["status"] == "confirmed"
            )
            assert len(previous["items"][0]["applicability"]["sources"]) == 1
            for offset in (0, 1, 10000):
                page = browser.get(
                    "/api/v1/catalog/items",
                    params={"model_id": str(c.model), "offset": offset},
                ).json()
                assert page["total"] == 0 and page["items"] == []
            card = browser.get(
                f"/api/v1/catalog/items/{c.item}", params={"model_id": str(c.model)}
            ).json()
            assert card["applicability"] == {
                "model_id": str(c.model),
                "status": "unknown",
                "sources": [],
            }
    finally:
        finish.set()
        event.remove(database.engine, "after_cursor_execute", pause)


def test_actual_cli_workflow_and_failed_commit(
    database: Database, pipeline: FilePipeline, tmp_path: Path
) -> None:
    with database.unit_of_work() as session:
        c = corpus(session, visibility="internal")
        review, release = proofs(session, c, tmp_path)
    pair = ["--item", str(c.item), "--model", str(c.model)]
    exit_code, output = cli_request(
        pipeline,
        c.login,
        ["applicability-publish", *pair, "--evidence", str(release)],
        commit_error=True,
    )
    assert exit_code == 1 and b"OPERATION_FAILED_ROLLED_BACK" in output
    assert b'"code": "PUBLISHED"' not in output
    for arguments, expected in (
        (["applicability-candidate", *pair, "--section", str(c.section)], "NO_OP"),
        (["applicability-review", *pair, "--evidence", str(review)], "NO_OP"),
        (["applicability-publish", *pair, "--evidence", str(release)], "PUBLISHED"),
        (["applicability-preview", *pair], "PREVIEW"),
        (["applicability-revoke", *pair], "REVOKED"),
    ):
        code, output = cli_request(pipeline, c.login, arguments)
        assert code == 0 and expected.encode() in output
        assert b"INTERNAL-SYNTHETIC-TEXT" not in output
        assert b"INTERNAL-SYNTHETIC-TITLE" not in output
    with database.unit_of_work() as session:
        assert status(session, c) == "unknown"


def test_model_archive_invalidates_permission_and_revision(
    session: Session, tmp_path: Path
) -> None:
    c = corpus(session)
    confirmed(session, c, tmp_path)
    before = service(session).item(c.item, c.model).revision
    actor = authenticate_operator(session, c.login, PASSWORD)
    model = session.get(EquipmentModel, c.model)
    assert actor and model
    token = StaffSession(
        user_id=actor.user.id,
        token_hash="d" * 64,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    session.add(token)
    session.flush()
    CatalogWriteService(session, StaffIdentity(actor.user, token)).change_model_state(
        c.model, ModelState(expected_updated_at=model.updated_at), "archive"
    )
    assert session.get(CatalogItem, c.item).revision == before + 1  # type: ignore[union-attr]
    assert not session.get(CatalogItemModel, (c.item, c.model)).public_allowed  # type: ignore[union-attr]
    assert service(session).items(CatalogQuery(model_id=c.model)).total == 0


def test_invalidation_preserves_orders_and_detects_stale_revision(
    database: Database, pipeline: FilePipeline, tmp_path: Path
) -> None:
    with database.unit_of_work() as session:
        c = corpus(session)
        confirmed(session, c, tmp_path)
        revision = service(session).item(c.item, c.model).revision
        session.execute(
            text(
                "UPDATE app.consent_texts SET retired_at=clock_timestamp() "
                "WHERE retired_at IS NULL"
            )
        )
        consent_version = "SYNTHETIC024-" + uuid4().hex
        session.add(
            ConsentText(
                version=consent_version,
                purpose="SYNTHETIC ONLY",
                text="SYNTHETIC CONSENT",
                checksum=hashlib.sha256(b"SYNTHETIC CONSENT").hexdigest(),
                active_from=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
    request = OrderCreate.model_validate(
        {
            "kind": "commercial",
            "items": [
                {"item_id": str(c.item), "qty": 1, "expected_revision": revision}
            ],
            "contact": {"name": "SYNTHETIC TEST", "email": "synthetic@example.invalid"},
            "consent": {"version": consent_version, "accepted": True},
            "confirmed": True,
            "source": "site",
        }
    )
    headers = OrderHeaders.model_validate(
        {"Idempotency-Key": str(uuid4()), "X-Request-Token": "a" * 64}
    )
    security = OrderSecurity(b"SYNTHETIC024-ONLY-SECURITY-KEY-32-BYTES")
    with database.unit_of_work() as session:
        receipt = create_order(session, request, headers, security)
    tables = (
        "orders",
        "order_items",
        "consents",
        "consent_texts",
        "order_status_history",
        "email_outbox",
    )
    with database.unit_of_work() as session:
        before = {
            t: session.execute(text(f"SELECT * FROM app.{t} ORDER BY 1")).all()
            for t in tables
        }
    with database.unit_of_work() as session:
        assert operation(session, c.login, pipeline, "revoke", c.document).ok
    with database.unit_of_work() as session:
        assert service(session).item(c.item, c.model).revision == revision + 1
        with pytest.raises(OrderRejected) as error:
            create_order(
                session,
                request,
                OrderHeaders.model_validate(
                    {"Idempotency-Key": str(uuid4()), "X-Request-Token": "b" * 64}
                ),
                security,
            )
        assert error.value.code == "CATALOG_CHANGED"
        assert replay_order(session, request, headers, security) == receipt
        for table in tables:
            assert (
                session.execute(text(f"SELECT * FROM app.{table} ORDER BY 1")).all()
                == before[table]
            )


def test_replacement_races_revoke_without_restoring_permission(
    database: Database, pipeline: FilePipeline, tmp_path: Path
) -> None:
    with database.unit_of_work() as session:
        c = corpus(session)
        confirmed(session, c, tmp_path)
        other = corpus(session)
        before = service(session).item(c.item, c.model).revision
    with database.unit_of_work() as holder:
        assert command(holder, c, "replace", section=other.section) == "REPLACED"
        with ThreadPoolExecutor(max_workers=1) as pool:

            def revoke() -> str:
                with database.unit_of_work() as session:
                    return operation(
                        session, c.login, pipeline, "revoke", c.document
                    ).code

            pending = pool.submit(revoke)
            try:
                wait_for_lock(database)
            finally:
                holder.commit()
            assert pending.result(timeout=10) == "REVOKED"
    with database.unit_of_work() as session:
        assert status(session, c) == "unknown"
        assert service(session).item(c.item, c.model).revision == before + 1
        link = session.get(CatalogItemModel, (c.item, c.model))
        assert (
            link
            and link.evidence_section_id == other.section
            and link.approved_at is None
        )


def test_changed_private_attestation_rolls_back_publication(
    session: Session, tmp_path: Path
) -> None:
    c = corpus(session)
    _, release = proofs(session, c, tmp_path)
    revision = service(session).item(c.item, c.model).revision

    def change_input(mapper: object, connection: object, target: AuditEvent) -> None:
        if target.action == "applicability.publish":
            release.write_text(release.read_text() + "\n")

    event.listen(AuditEvent, "after_insert", change_input)
    try:
        assert command(session, c, "publish", proof=release) == "MANIFEST_CHANGED"
    finally:
        event.remove(AuditEvent, "after_insert", change_input)
    assert status(session, c) == "unknown"
    assert service(session).item(c.item, c.model).revision == revision
    assert not session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.action == "applicability.publish",
            AuditEvent.entity_id == c.item,
            AuditEvent.result == "success",
        )
    )
