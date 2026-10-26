"""Synthetic knowledge only; actual PostgreSQL constraints, locks and Russian FTS."""

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff_models import AuditEvent, StaffSession, StaffUser
from app.core.staff_sessions import StaffIdentity
from app.modules.catalog.models import EquipmentModel
from app.modules.kb.draft_service import (
    DraftEditRejected,
    DraftEditService,
    DraftFile,
    SectionContent,
)
from app.modules.kb.models import Document, DocumentSection, DocumentVersion

pytestmark = pytest.mark.integration


@pytest.fixture
def kb_session(databases: tuple[Database, Database]) -> Iterator[Session]:
    # Roll back test writes, including immutable editions and append-only audit.
    # No trigger disabling, destructive cleanup or special runtime bypass flags.
    with databases[0].engine.connect() as connection, connection.begin() as transaction:
        with Session(connection, expire_on_commit=False) as session:
            yield session
        transaction.rollback()


def document(session: Session, **fields: object) -> Document:
    row = Document(
        **(
            {
                "code": "SYNTHETIC-" + uuid4().hex,
                "title": "Синтетический документ",
                "kind": "manual",
                "visibility": "internal",
            }
            | fields
        )
    )
    session.add(row)
    session.flush()
    return row


def edition(
    session: Session, document_identifier: UUID, **fields: object
) -> DocumentVersion:
    row = DocumentVersion(
        **(
            {
                "document_id": document_identifier,
                "version": 1,
                "file_key": uuid4().hex,
                "mime": "text/plain",
                "size_bytes": 128,
                "checksum_sha256": "a" * 64,
            }
            | fields
        )
    )
    session.add(row)
    session.flush()
    return row


def section(
    session: Session, version_identifier: UUID, **fields: object
) -> DocumentSection:
    row = DocumentSection(
        **(
            {
                "document_version_id": version_identifier,
                "section_key": "synthetic-" + uuid4().hex,
                "title": "Синтетическое руководство",
                "text": "Синтетическая проверка насосов и оборудования.",
            }
            | fields
        )
    )
    session.add(row)
    session.flush()
    return row


def actor(session: Session, role: str = "admin", active: bool = True) -> StaffIdentity:
    user = StaffUser(
        login="synthetic-kb-" + uuid4().hex,
        password_hash="$argon2id$synthetic",
        role=role,
        is_active=active,
    )
    session.add(user)
    session.flush()
    login = StaffSession(
        user_id=user.id,
        token_hash=uuid4().hex * 2,
        expires_at=utc_now() + timedelta(hours=1),
    )
    session.add(login)
    session.flush()
    return StaffIdentity(user, login)


@pytest.mark.parametrize("kind", ["manual", "regulation", "article", "other"])
@pytest.mark.parametrize("visibility", ["public", "internal"])
def test_document_defaults_and_kinds(
    kb_session: Session, kind: str, visibility: str
) -> None:
    row = document(kb_session, kind=kind, visibility=visibility)
    assert row.id.version == 4 and row.status == "active"
    assert row.created_at.utcoffset() == timedelta(0)
    kb_session.execute(
        update(Document).where(Document.id == row.id).values(status="revoked")
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "invalid"},
        {"status": "published"},
        {"visibility": "secret"},
        {"code": " "},
        {"title": ""},
        {"kind": None},
        {"visibility": None},
        {"code": None},
        {"title": None},
    ],
)
def test_invalid_document(kb_session: Session, fields: dict[str, object]) -> None:
    with pytest.raises(IntegrityError), kb_session.begin_nested():
        document(kb_session, **fields)


def test_document_code_unique(kb_session: Session) -> None:
    row = document(kb_session)
    with (
        pytest.raises(IntegrityError, match="uq_documents_code"),
        kb_session.begin_nested(),
    ):
        document(kb_session, code=row.code)


@pytest.mark.parametrize(
    "fields",
    [
        {"version": 0},
        {"version": -1},
        {"version": None},
        {"status": "active"},
        {"document_id": uuid4()},
        {"document_id": None},
        {"reviewed_by": uuid4()},
        {"size_bytes": -1},
        {"size_bytes": 0},
        {"size_bytes": 26214401},
        {"size_bytes": None},
        {"checksum_sha256": "A" * 64},
        {"checksum_sha256": "g" * 64},
        {"checksum_sha256": "a" * 63},
        {"checksum_sha256": None},
        {"file_key": "../synthetic.txt"},
        {"file_key": "/synthetic"},
        {"file_key": ""},
        {"file_key": "https://example.invalid/file"},
        {"mime": " "},
        {"mime": None},
    ],
)
def test_invalid_version(kb_session: Session, fields: dict[str, object]) -> None:
    row = document(kb_session)
    with pytest.raises(IntegrityError), kb_session.begin_nested():
        edition(kb_session, row.id, **fields)


def test_versions_unique_and_review_fk(kb_session: Session) -> None:
    doc = document(kb_session)
    reviewer = actor(kb_session)
    one = edition(kb_session, doc.id)
    assert (
        one.reviewed_by is None and one.approved_at is None and one.published_at is None
    )
    two = edition(
        kb_session,
        doc.id,
        version=2,
        reviewed_by=reviewer.user.id,
        approved_at=utc_now(),
        published_at=utc_now(),
    )
    assert two.approved_at is not None and two.approved_at.utcoffset() == timedelta(0)
    with (
        pytest.raises(IntegrityError, match="uq_document_versions_document_id"),
        kb_session.begin_nested(),
    ):
        edition(kb_session, doc.id)
    for i in (3, 4):
        edition(kb_session, doc.id, version=i, status="retired")
    edition(kb_session, doc.id, version=5, status="published")
    with (
        pytest.raises(IntegrityError, match="ix_document_versions_published"),
        kb_session.begin_nested(),
    ):
        edition(kb_session, doc.id, version=6, status="published")


@pytest.mark.parametrize(
    "pages", [(0, 1), (-1, 2), (2, 1), (None, 1), (1, None), (0, 0)]
)
def test_invalid_pages(
    kb_session: Session, pages: tuple[int | None, int | None]
) -> None:
    version = edition(kb_session, document(kb_session).id)
    with (
        pytest.raises(IntegrityError, match="ck_document_sections_pages"),
        kb_session.begin_nested(),
    ):
        section(kb_session, version.id, page_from=pages[0], page_to=pages[1])


@pytest.mark.parametrize("pages", [(None, None), (1, 1), (1, 10)])
def test_sections_unique_and_source_identity(
    kb_session: Session, pages: tuple[int | None, int | None]
) -> None:
    doc = document(kb_session)
    version = edition(kb_session, doc.id)
    part = section(kb_session, version.id, page_from=pages[0], page_to=pages[1])
    assert part.assistant_allowed is False
    with (
        pytest.raises(IntegrityError, match="uq_document_sections_document_version_id"),
        kb_session.begin_nested(),
    ):
        section(kb_session, version.id, section_key=part.section_key)
    newer = edition(kb_session, doc.id, version=2)
    section(kb_session, newer.id, section_key=part.section_key)
    assert kb_session.execute(
        select(DocumentSection.id, DocumentVersion.id, Document.id)
        .join(
            DocumentVersion, DocumentSection.document_version_id == DocumentVersion.id
        )
        .join(Document, DocumentVersion.document_id == Document.id)
        .where(DocumentSection.id == part.id)
    ).one() == (part.id, version.id, doc.id)
    assert "document_id" not in DocumentSection.__table__.columns
    for model, identifier, constraint in (
        (Document, doc.id, "fk_document_versions_document_id_documents"),
        (
            DocumentVersion,
            version.id,
            "fk_document_sections_document_version_id_document_versions",
        ),
    ):
        with pytest.raises(IntegrityError, match=constraint), kb_session.begin_nested():
            kb_session.execute(delete(model).where(model.id == identifier))


@pytest.mark.parametrize(
    "fields",
    [
        {"document_version_id": uuid4()},
        {"section_key": " "},
        {"section_key": None},
        {"title": None},
        {"text": None},
        {"assistant_allowed": None},
    ],
)
def test_invalid_section(kb_session: Session, fields: dict[str, object]) -> None:
    version = edition(kb_session, document(kb_session).id)
    with pytest.raises(IntegrityError), kb_session.begin_nested():
        kb_session.execute(
            insert(DocumentSection).values(
                **(
                    {
                        "document_version_id": version.id,
                        "section_key": "synthetic",
                        "title": "Synthetic",
                        "text": "Synthetic",
                        "assistant_allowed": False,
                    }
                    | fields
                )
            )
        )


def test_equipment_manual_fk(kb_session: Session) -> None:
    doc = document(kb_session)
    model = EquipmentModel(
        code="SYNTHETIC-" + uuid4().hex,
        name="Synthetic",
        description="Synthetic",
        manual_document_id=doc.id,
    )
    kb_session.add(model)
    kb_session.flush()
    with (
        pytest.raises(
            IntegrityError, match="fk_equipment_models_manual_document_id_documents"
        ),
        kb_session.begin_nested(),
    ):
        kb_session.execute(delete(Document).where(Document.id == doc.id))
    with pytest.raises(IntegrityError), kb_session.begin_nested():
        kb_session.execute(
            update(EquipmentModel)
            .where(EquipmentModel.id == model.id)
            .values(manual_document_id=uuid4())
        )
    kb_session.execute(
        update(EquipmentModel)
        .where(EquipmentModel.id == model.id)
        .values(manual_document_id=None)
    )
    kb_session.execute(delete(Document).where(Document.id == doc.id))


@pytest.fixture
def kb_owner_session(databases: tuple[Database, Database]) -> Iterator[Session]:
    with databases[1].engine.connect() as connection, connection.begin() as transaction:
        with Session(connection, expire_on_commit=False) as session:
            yield session
        transaction.rollback()


def test_future_source_fk_restricts_draft_delete(kb_owner_session: Session) -> None:
    kb_session = kb_owner_session
    # Transactional synthetic probe; fixture rollback removes the test-only DDL.
    kb_session.execute(
        text(
            "CREATE TABLE app.synthetic_source_ref (section_id uuid "
            "REFERENCES app.document_sections(id) ON DELETE RESTRICT)"
        )
    )
    part = section(kb_session, edition(kb_session, document(kb_session).id).id)
    kb_session.execute(
        text("INSERT INTO app.synthetic_source_ref VALUES (:id)"), {"id": part.id}
    )
    with pytest.raises(IntegrityError), kb_session.begin_nested():
        kb_session.execute(delete(DocumentSection).where(DocumentSection.id == part.id))
    kb_session.execute(text("DELETE FROM app.synthetic_source_ref"))
    kb_session.execute(delete(DocumentSection).where(DocumentSection.id == part.id))


def test_russian_fts_generated_and_gin(kb_session: Session) -> None:
    # A conflicting session default proves the generated expression is explicit.
    kb_session.execute(text("SET LOCAL default_text_search_config = 'english'"))
    part = section(kb_session, edition(kb_session, document(kb_session).id).id)
    assert (
        kb_session.scalar(
            text(
                "SELECT to_tsvector('russian', 'насосы') = "
                "to_tsvector('russian', 'насосом')"
            )
        )
        is True
    )
    query = text(
        "SELECT id FROM app.document_sections WHERE id=:id AND "
        "search_vector @@ websearch_to_tsquery('russian', :q)"
    )
    assert kb_session.scalar(query, {"id": part.id, "q": "насос"}) == part.id
    assert kb_session.scalar(query, {"id": part.id, "q": "руководства"}) == part.id
    kb_session.execute(
        update(DocumentSection)
        .where(DocumentSection.id == part.id)
        .values(title="Синтетические фильтры", text="Синтетическая проверка фильтров")
    )
    assert kb_session.scalar(query, {"id": part.id, "q": "насос"}) is None
    assert kb_session.scalar(query, {"id": part.id, "q": "фильтрами"}) == part.id
    kb_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = " ".join(
        kb_session.scalars(
            text(
                "EXPLAIN SELECT id FROM app.document_sections "
                "WHERE search_vector @@ websearch_to_tsquery('russian', 'фильтрами')"
            )
        )
    )
    assert "ix_document_sections_search_vector" in plan
    with pytest.raises(DBAPIError), kb_session.begin_nested():
        kb_session.execute(
            text(
                "UPDATE app.document_sections SET search_vector=''::tsvector "
                "WHERE id=:id"
            ),
            {"id": part.id},
        )


@pytest.mark.parametrize("state", ["published", "retired", "revoked"])
def test_service_boundary_and_db_immutability(kb_session: Session, state: str) -> None:
    identity = actor(kb_session)
    service = DraftEditService(kb_session, identity)
    version = edition(kb_session, document(kb_session).id)
    part = section(kb_session, version.id)
    content = SectionContent(
        "Синтетический фильтр", "Синтетические фильтры", None, None, True
    )
    service.edit_section(part.id, content)
    service.replace_file(
        version.id, DraftFile(uuid4().hex, "text/plain", 256, "b" * 64)
    )
    assert part.text == content.text and version.size_bytes == 256
    events = kb_session.scalars(
        select(AuditEvent).where(AuditEvent.actor_id == identity.user.id)
    ).all()
    assert len(events) == 2 and all(event.details == {} for event in events)
    kb_session.execute(
        update(DocumentVersion)
        .where(DocumentVersion.id == version.id)
        .values(
            status=state,
            reviewed_by=identity.user.id,
            approved_at=utc_now(),
            published_at=utc_now(),
        )
    )
    edits: tuple[Callable[[], None], ...] = (
        lambda: service.edit_section(part.id, content),
        lambda: service.replace_file(
            version.id, DraftFile(uuid4().hex, "text/plain", 512, "c" * 64)
        ),
    )
    for edit in edits:
        with (
            pytest.raises(DraftEditRejected, match="Only active document drafts"),
            kb_session.begin_nested(),
        ):
            edit()
    # Every immutable field is tested with an otherwise valid replacement.
    for field, value in {
        "id": uuid4(),
        "document_id": document(kb_session).id,
        "version": 9,
        "file_key": uuid4().hex,
        "mime": "application/pdf",
        "size_bytes": 512,
        "checksum_sha256": "c" * 64,
        "reviewed_by": actor(kb_session).user.id,
        "approved_at": utc_now(),
        "published_at": utc_now(),
        "created_at": utc_now(),
        "status": "draft",
    }.items():
        with pytest.raises(IntegrityError), kb_session.begin_nested():
            kb_session.execute(
                update(DocumentVersion)
                .where(DocumentVersion.id == version.id)
                .values(**{field: value})
            )
    for field, value in {
        "id": uuid4(),
        "document_version_id": edition(kb_session, document(kb_session).id).id,
        "section_key": "another-key",
        "title": "changed",
        "text": "changed",
        "page_from": 1,
        "assistant_allowed": False,
    }.items():
        with pytest.raises(IntegrityError), kb_session.begin_nested():
            kb_session.execute(
                update(DocumentSection)
                .where(DocumentSection.id == part.id)
                .values(**{field: value})
            )
    with pytest.raises(IntegrityError), kb_session.begin_nested():
        section(kb_session, version.id)
    with pytest.raises(IntegrityError), kb_session.begin_nested():
        kb_session.execute(delete(DocumentSection).where(DocumentSection.id == part.id))
    # Standalone editions are permanent even without a section FK.
    standalone = edition(kb_session, document(kb_session).id, status=state)
    with pytest.raises(IntegrityError), kb_session.begin_nested():
        kb_session.execute(
            delete(DocumentVersion).where(DocumentVersion.id == standalone.id)
        )


@pytest.mark.parametrize("target", ["retired", "revoked"])
def test_future_lifecycle_status_only_remains_possible(
    kb_session: Session, target: str
) -> None:
    row = edition(kb_session, document(kb_session).id, status="published")
    kb_session.execute(
        update(DocumentVersion)
        .where(DocumentVersion.id == row.id)
        .values(status=target)
    )
    if target == "revoked":
        with pytest.raises(IntegrityError), kb_session.begin_nested():
            kb_session.execute(
                update(DocumentVersion)
                .where(DocumentVersion.id == row.id)
                .values(status="published")
            )


@pytest.mark.parametrize("role,active", [("manager", True), ("admin", False)])
def test_service_requires_active_admin(
    kb_session: Session, role: str, active: bool
) -> None:
    with pytest.raises(DraftEditRejected, match="Active admin required"):
        DraftEditService(kb_session, actor(kb_session, role, active))


def test_draft_edit_rollback_and_revoked_document(kb_session: Session) -> None:
    identity = actor(kb_session)
    doc = document(kb_session)
    version = edition(kb_session, doc.id)
    part = section(kb_session, version.id)
    before = part.text
    service = DraftEditService(kb_session, identity)
    with pytest.raises(IntegrityError), kb_session.begin_nested():
        service.edit_section(
            part.id, SectionContent("Synthetic", "rolled back", None, None, True)
        )
        service.replace_file(
            version.id, DraftFile("../invalid", "text/plain", 128, "a" * 64)
        )
    kb_session.refresh(part)
    assert part.text == before
    assert (
        kb_session.scalar(
            select(AuditEvent.id).where(AuditEvent.actor_id == identity.user.id)
        )
        is None
    )
    kb_session.execute(
        update(Document).where(Document.id == doc.id).values(status="revoked")
    )
    with pytest.raises(DraftEditRejected):
        service.edit_section(
            part.id, SectionContent("Synthetic", "refused", None, None, True)
        )
    with pytest.raises(DraftEditRejected):
        service.replace_file(
            uuid4(), DraftFile(uuid4().hex, "text/plain", 128, "a" * 64)
        )
    with pytest.raises(DraftEditRejected):
        service.edit_section(
            uuid4(), SectionContent("Synthetic", "missing", None, None, False)
        )


def test_pg_catalog_constraints_indexes_and_grants(kb_session: Session) -> None:
    indexes = {
        row.indexname: row.indexdef
        for row in kb_session.execute(
            text(
                "SELECT indexname,indexdef FROM pg_indexes "
                "WHERE schemaname='app' AND tablename LIKE 'document%' "
            )
        ).all()
    }
    assert "USING gin (search_vector)" in indexes["ix_document_sections_search_vector"]
    assert (
        "USING btree (document_version_id)"
        in indexes["ix_document_sections_document_version_id"]
    )
    published = indexes["ix_document_versions_published"]
    assert (
        "UNIQUE INDEX" in published
        and "WHERE" in published
        and "published" in published
    )
    constraints = kb_session.execute(
        text(
            "SELECT c.conname,c.contype,c.confdeltype "
            "FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace "
            "WHERE n.nspname='app' AND (c.conrelid IN ('app.documents'::regclass,"
            "'app.document_versions'::regclass,'app.document_sections'::regclass) "
            "OR c.conname='fk_equipment_models_manual_document_id_documents')"
        )
    ).all()
    assert len([c for c in constraints if c.contype == "f"]) == 4
    assert all(c.confdeltype == "r" for c in constraints if c.contype == "f")
    for name in (
        "ck_document_versions_version",
        "ck_document_versions_size_bytes",
        "ck_document_versions_checksum_sha256",
        "ck_document_sections_pages",
        "uq_documents_code",
        "uq_document_versions_document_id",
        "uq_document_sections_document_version_id",
    ):
        assert name in {c.conname for c in constraints}
    for table in ("documents", "document_versions", "document_sections"):
        assert kb_session.scalar(
            text(
                "SELECT has_table_privilege(current_user,:table,"
                "'SELECT,INSERT,UPDATE,DELETE')"
            ),
            {"table": "app." + table},
        )
    assert not kb_session.scalar(
        text("SELECT has_schema_privilege(current_user,'app','CREATE')")
    )
    expression = kb_session.scalar(
        text(
            "SELECT pg_get_expr(d.adbin,d.adrelid) FROM pg_attrdef d "
            "JOIN pg_attribute a ON a.attrelid=d.adrelid AND a.attnum=d.adnum "
            "WHERE d.adrelid='app.document_sections'::regclass "
            "AND a.attname='search_vector'"
        )
    )
    assert "russian" in expression and "title" in expression and "text" in expression


def test_concurrent_publication_blocks_section_write(
    databases: tuple[Database, Database],
) -> None:
    runtime = databases[0]
    with runtime.unit_of_work() as session:
        doc = document(session)
        version = edition(session, doc.id)
        part = section(session, version.id)
        part_id, version_id = part.id, version.id
    started = Event()

    def edit() -> None:
        with runtime.unit_of_work() as session:
            started.set()
            session.execute(
                update(DocumentSection)
                .where(DocumentSection.id == part_id)
                .values(text="must not persist")
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with runtime.unit_of_work() as publisher:
            publisher.execute(
                update(DocumentVersion)
                .where(DocumentVersion.id == version_id)
                .values(status="published")
            )
            future = pool.submit(edit)
            assert started.wait(2)
            # Verify that PostgreSQL sees the writer waiting on the parent lock.
            for _ in range(200):
                with runtime.engine.connect() as connection:
                    waiting = connection.scalar(
                        text(
                            "SELECT EXISTS (SELECT FROM pg_stat_activity "
                            "WHERE datname=current_database() "
                            "AND wait_event_type='Lock' "
                            "AND query LIKE 'UPDATE app.document_sections%')"
                        )
                    )
                if waiting:
                    break
            assert waiting and not future.done()
        with pytest.raises(IntegrityError, match="Only draft sections"):
            future.result(timeout=5)
    with runtime.unit_of_work() as session:
        assert (
            session.scalar(
                select(DocumentSection.text).where(DocumentSection.id == part_id)
            )
            != "must not persist"
        )


def test_service_refreshes_stale_draft_after_publication(
    databases: tuple[Database, Database],
) -> None:
    runtime = databases[0]
    with runtime.unit_of_work() as session:
        version = edition(session, document(session).id)
        part = section(session, version.id)
        version_id, section_id = version.id, part.id
    with runtime.engine.connect() as connection, connection.begin() as transaction:
        with Session(connection, expire_on_commit=False) as editor:
            cached = editor.get(DocumentVersion, version_id)
            assert cached is not None and cached.status == "draft"
            service = DraftEditService(editor, actor(editor))
            with runtime.unit_of_work() as publisher:
                publisher.execute(
                    update(DocumentVersion)
                    .where(DocumentVersion.id == version_id)
                    .values(status="published")
                )
            assert cached.status == "draft"
            with pytest.raises(DraftEditRejected):
                service.edit_section(
                    section_id,
                    SectionContent("Synthetic", "refused", None, None, False),
                )
            assert cached.status == "published"
        transaction.rollback()


def test_reviewer_delete_restricts_without_other_references(
    kb_owner_session: Session,
) -> None:
    reviewer = actor(kb_owner_session)
    edition(
        kb_owner_session, document(kb_owner_session).id, reviewed_by=reviewer.user.id
    )
    kb_owner_session.execute(
        delete(StaffSession).where(StaffSession.user_id == reviewer.user.id)
    )
    with (
        pytest.raises(
            IntegrityError, match="fk_document_versions_reviewed_by_staff_users"
        ),
        kb_owner_session.begin_nested(),
    ):
        kb_owner_session.execute(
            delete(StaffUser).where(StaffUser.id == reviewer.user.id)
        )
