"""MVP-032 on synthetic PostgreSQL 16: ACL/FTS, pins and public actions."""

import secrets
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from test_catalog_applicability import corpus
from test_kb_import import session as session
from test_tree_lifecycle import prepared, run

from app.core.csrf import SESSION_COOKIE
from app.core.db_types import utc_now
from app.core.staff_models import StaffSession
from app.core.staff_sessions import token_hash
from app.main import app
from app.modules.catalog.models import CatalogItem, CatalogItemModel
from app.modules.chat.actions import ActionRejected, ActionValidator
from app.modules.chat.diagnosis import DiagnosisState
from app.modules.chat.mode_router import ModeRouter
from app.modules.kb.access import PUBLIC_VIEWER, cookie_viewer
from app.modules.kb.models import Document, DocumentSection, DocumentVersion
from app.modules.kb.read_service import KnowledgeReadService
from app.modules.kb.tree_models import DiagnosticTree
from app.modules.kb.tree_reads import published_scenarios
from app.schemas.chat import (
    ChatReply,
    OpenDocumentAction,
    SelectModeInput,
    StartDiagnosisInput,
)
from app.schemas.sources import KnowledgeQuery, SourceRef

pytestmark = pytest.mark.integration


def edition(
    session: Session,
    reviewer: UUID,
    *,
    visibility: str = "public",
    status: str = "published",
    allowed: bool = True,
    active: bool = True,
    content: str = "Синтетический квантопылесос. Точная цитата.",
) -> SourceRef:
    document = Document(
        code="SYNTHETIC032-" + uuid4().hex,
        title="SYNTHETIC 032",
        kind="other",
        visibility=visibility,
    )
    session.add(document)
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        version=1,
        file_key="Synthetic032File",
        mime="text/plain",
        size_bytes=20,
        checksum_sha256="a" * 64,
    )
    session.add(version)
    session.flush()
    section = DocumentSection(
        document_version_id=version.id,
        section_key="intro",
        title="SYNTHETIC SECTION",
        text=content,
        assistant_allowed=allowed,
    )
    session.add(section)
    session.flush()
    if status != "draft":
        version.reviewed_by = reviewer
        version.approved_at = version.published_at = utc_now()
        version.status = "published"
        session.flush()
        version.status = status
    if not active:
        document.status = "revoked"
    session.flush()
    from app.modules.kb.read_service import source_ref

    return source_ref(document, version, section)


def assert_contract(reply: ChatReply) -> None:
    assert ChatReply.model_validate_json(reply.model_dump_json()) == reply
    assert {a.type for a in reply.actions} <= {
        "open_item",
        "open_document",
        "open_request_form",
        "contact_specialist",
    }


def test_knowledge_fts_five_exact_quotes_and_staff_cookie(session: Session) -> None:
    c = corpus(session)
    public = [
        edition(session, c.actor, content=f"Квантопылесос: точная цитата {i}.")
        for i in range(7)
    ]
    hidden = [
        edition(session, c.actor, visibility="internal"),
        edition(session, c.actor, allowed=False),
        edition(session, c.actor, status="draft"),
        edition(session, c.actor, status="retired"),
        edition(session, c.actor, status="revoked"),
        edition(session, c.actor, active=False),
    ]
    token = secrets.token_hex(32)
    session.add(
        StaffSession(
            user_id=c.actor,
            token_hash=token_hash(token),
            expires_at=utc_now() + timedelta(hours=1),
        )
    )
    session.flush()
    # Prove the cookie really grants internal knowledge to the staff service.
    staff = KnowledgeReadService(session, cookie_viewer(token))
    assert staff.search(KnowledgeQuery(q="квантопылесос")).total == 8
    assert (
        KnowledgeReadService(session, PUBLIC_VIEWER)
        .search(KnowledgeQuery(q="квантопылесос"))
        .total
        == 7
    )
    state = DiagnosisState(mode="knowledge")
    replies = []
    with TestClient(app) as browser:
        for cookie in (None, token):
            browser.cookies.clear()
            if cookie:
                browser.cookies.set(SESSION_COOKIE, cookie)
            router = ModeRouter(session)  # Has no request/cookie/viewer argument.
            result = router.route(state, {"type": "text", "value": "квантопылесос"})
            replies.append(result.reply)
    assert replies[0] == replies[1]
    reply = replies[0]
    assert reply.resolution == "results" and len(reply.sources) == 5
    assert all(s in public for s in reply.sources)
    assert not any(str(s.section_id) in reply.model_dump_json() for s in hidden)
    for index, source in enumerate(reply.sources, start=1):
        excerpt = session.scalar(
            select(DocumentSection.text).where(DocumentSection.id == source.section_id)
        )
        assert f"[{index}] {excerpt}" in reply.text
    assert [
        a.payload for a in reply.actions if isinstance(a, OpenDocumentAction)
    ] == reply.sources
    assert_contract(reply)


@pytest.mark.parametrize("mode", ["knowledge", "commerce"])
def test_no_match_menu_without_automatic_mode_change(
    session: Session, mode: str
) -> None:
    state = DiagnosisState(mode=mode)  # type: ignore[arg-type]
    result = ModeRouter(session).route(state, {"type": "text", "value": uuid4().hex})
    assert result.state == state and result.reply.resolution == "no_match"
    assert len(result.reply.buttons) == 3
    assert result.reply.actions[0].type == "contact_specialist"
    assert_contract(result.reply)


@pytest.mark.parametrize("kind", ["product", "service"])
def test_commerce_current_prices_images_and_unknown(
    session: Session, tmp_path: Path, kind: str
) -> None:
    c, tree_id, path, proof = prepared(session, tmp_path, kind=kind)
    item = session.get(CatalogItem, c.item)
    assert item
    item.name = "SYNTHETIC032 " + uuid4().hex
    item.price_mode, item.price, item.image_key = (
        "fixed",
        Decimal("123.45"),
        "a" * 64 + ".png",
    )
    session.flush()
    router = ModeRouter(session)
    state = DiagnosisState(mode="commerce")
    result = router.route(state, {"type": "text", "value": item.name}, model_id=c.model)
    card = result.reply.items[0]
    assert card.price == Decimal("123.45")
    assert (
        card.kind == kind
        and card.applicability
        and card.applicability.status == "confirmed"
    )
    assert (
        card.image_url == f"/api/v1/catalog/items/{c.item}/image/" + "a" * 64 + ".png"
    )
    form = next(a for a in result.reply.actions if a.type == "open_request_form")
    assert form.payload.kind == "commercial" and form.payload.items[0].qty == 1
    assert_contract(result.reply)
    item.price_mode, item.price, item.image_key = "on_request", None, None
    item.revision += 1
    # New observation on the same Session must not retain any old card or flag.
    session.execute(
        update(CatalogItemModel)
        .where(CatalogItemModel.item_id == c.item)
        .values(public_allowed=False, published_by=None, published_at=None)
    )
    session.flush()
    again = router.route(state, {"type": "text", "value": item.sku}, model_id=c.model)
    current = again.reply.items[0]
    assert current.price is None and current.price_mode == "on_request"
    assert current.revision == card.revision + 1 and current.image_url is None
    assert current.applicability and current.applicability.status == "unknown"
    assert current.applicability.sources == []
    for status in ("archived", "draft"):
        item.status = status
        session.flush()
        assert (
            router.route(state, {"type": "text", "value": item.sku}).reply.resolution
            == "no_match"
        )
        with pytest.raises(ActionRejected):
            router.actions.validate(
                {"type": "open_item", "payload": {"item_id": str(c.item)}}
            )


def test_diagnosis_scenarios_options_feedback_and_retired_pin(
    session: Session, tmp_path: Path
) -> None:
    c, one, path, proof = prepared(session, tmp_path)
    tree = session.get(DiagnosticTree, one)
    assert tree
    key = tree.key
    assert published_scenarios(session, c.model) == []
    assert run(session, c, one, "publish", path, proof).ok
    router = ModeRouter(session)
    menu = router.route(
        DiagnosisState(), {"type": "text", "value": "SYNTHETIC"}, model_id=c.model
    )
    start = next(
        b.input for b in menu.reply.buttons if isinstance(b.input, StartDiagnosisInput)
    )
    started = router.route(menu.state, start)
    assert started.state.tree_id == one and started.state.current_node_key == "observe"
    assert started.reply.text == "SYNTHETIC choice"
    assert started.reply.sources[0].section_id == c.section
    for event in (
        {"type": "text", "value": "a"},
        {"type": "button", "option_id": "result"},
        {"type": "button", "option_id": "a", "next": "result"},
        {"type": "button", "option_id": "a", "node_id": "result"},
        {"type": "feedback", "useful": True},
    ):
        rejected = router.route(started.state, event)
        assert rejected.state == started.state
    _, two, path2, proof2 = prepared(session, tmp_path, c=c, key=key, version=2)
    assert run(session, c, two, "publish", path2, proof2).ok
    result = router.route(started.state, {"type": "button", "option_id": "a"})
    assert result.state.tree_id == one and result.state.transitions == 1
    assert result.reply.resolution == "results" and result.state.outcome == "unresolved"
    assert result.reply.items[0].id == c.item
    form = next(a for a in result.reply.actions if a.type == "open_request_form")
    assert form.payload.items[0].qty == 2
    resolved = router.route(result.state, {"type": "feedback", "useful": True})
    assert (
        resolved.state.outcome == "resolved"
        and resolved.reply.resolution == "completed"
    )
    assert (
        router.route(resolved.state, {"type": "feedback", "useful": False}).state
        == resolved.state
    )
    reset = router.route(result.state, {"type": "select_mode", "mode": "knowledge"})
    assert reset.state == DiagnosisState(mode="knowledge")
    assert all(isinstance(b.input, SelectModeInput) for b in reset.reply.buttons)
    assert_contract(result.reply)
    assert run(session, c, one, "revoke").ok
    blocked = router.route(started.state, {"type": "button", "option_id": "a"})
    assert blocked.reply.resolution == "handoff"
    assert not blocked.reply.sources and not blocked.reply.items


@pytest.mark.parametrize(
    "case", ["draft", "wrong_model", "missing", "internal", "revoked", "source_revoked"]
)
def test_unavailable_scenarios_handoff_without_leaks(
    session: Session, tmp_path: Path, case: str
) -> None:
    c, tree_id, path, proof = prepared(session, tmp_path)
    tree = session.get(DiagnosticTree, tree_id)
    assert tree
    if case != "draft":
        assert run(session, c, tree_id, "publish", path, proof).ok
    if case == "internal":
        session.execute(
            update(Document)
            .where(Document.id == c.document)
            .values(visibility="internal")
        )
    if case == "revoked":
        assert run(session, c, tree_id, "revoke").ok
    if case == "source_revoked":
        session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id == c.version)
            .values(status="revoked")
        )
    session.flush()
    result = ModeRouter(session).route(
        DiagnosisState(),
        {
            "type": "start_diagnosis",
            "tree_key": "MISSING" if case == "missing" else tree.key,
            "model_id": str(uuid4() if case == "wrong_model" else c.model),
        },
    )
    assert result.reply.resolution == "handoff" and result.state.tree_id is None
    assert not result.reply.sources and not result.reply.items
    assert "SYNTHETIC choice" not in result.reply.text
    if case not in {"wrong_model", "missing"}:
        assert published_scenarios(session, c.model) == []


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "internal",
        "draft",
        "revoked",
        "not_allowed",
        "mismatched_document",
        "mismatched_version",
        "title",
        "url",
    ],
)
def test_open_document_exact_public_source(session: Session, case: str) -> None:
    c = corpus(session)
    source = edition(
        session,
        c.actor,
        visibility="internal" if case == "internal" else "public",
        status=case if case in {"draft", "revoked"} else "published",
        allowed=case != "not_allowed",
    )
    validator = ActionValidator(session)
    raw = source.model_dump(mode="json")
    if case == "missing":
        raw["section_id"] = str(uuid4())
    if case == "mismatched_document":
        raw["document_id"] = str(uuid4())
        raw["url"] = (
            f"/api/v1/documents/{raw['document_id']}/versions/{raw['document_version_id']}/file"
        )
    if case == "mismatched_version":
        raw["document_version_id"] = str(uuid4())
        raw["url"] = (
            f"/api/v1/documents/{raw['document_id']}/versions/{raw['document_version_id']}/file"
        )
    if case == "title":
        raw["title"] = "<script>SYNTHETIC</script>"
    if case == "url":
        raw["url"] = "https://example.invalid/file"
    with pytest.raises(ActionRejected, match="ACTION_UNAVAILABLE"):
        validator.validate({"type": "open_document", "payload": raw})


def test_all_valid_actions_and_duplicates_are_read_only(session: Session) -> None:
    c = corpus(session)
    source = KnowledgeReadService(session, PUBLIC_VIEWER).get_source(c.section)
    actions = [
        {"type": "open_item", "payload": {"item_id": str(c.item)}},
        {"type": "open_document", "payload": source.model_dump(mode="json")},
        {
            "type": "open_request_form",
            "payload": {
                "kind": "commercial",
                "items": [{"item_id": str(c.item), "qty": 999}],
            },
        },
        {"type": "open_request_form", "payload": {"kind": "support", "items": []}},
        {"type": "contact_specialist", "payload": {"kind": "support"}},
    ]
    statements: list[str] = []

    def capture(
        session: Session, connection: object, statement: str, *args: object
    ) -> None:
        statements.append(statement.lstrip().split()[0].upper())

    connection = session.connection()
    sa_event.listen(connection, "before_cursor_execute", capture)
    try:
        for raw in actions:
            ActionValidator(session).validate(raw)
        for state in (
            DiagnosisState(),
            DiagnosisState(mode="knowledge"),
            DiagnosisState(mode="commerce"),
        ):
            result = ModeRouter(session).route(
                state, {"type": "text", "value": "SYNTHETIC"}
            )
            assert_contract(result.reply)
    finally:
        sa_event.remove(connection, "before_cursor_execute", capture)
    assert statements and set(statements) <= {"SELECT", "WITH"}
    validator = ActionValidator(session)
    for items in (
        [{"item_id": str(c.item), "qty": 1}] * 2,
        [{"item_id": str(uuid4()), "qty": 1}],
        [{"item_id": str(c.item), "qty": 1}] * 51,
    ):
        with pytest.raises(ActionRejected):
            validator.validate(
                {
                    "type": "open_request_form",
                    "payload": {"kind": "commercial", "items": items},
                }
            )


def test_transition_count_not_session_version(session: Session, tmp_path: Path) -> None:
    c, pin, path, proof = prepared(session, tmp_path)
    assert run(session, c, pin, "publish", path, proof).ok
    router = ModeRouter(session)
    state = DiagnosisState(tree_id=pin, current_node_key="observe", transitions=30)
    result = router.route(state, {"type": "button", "option_id": "a"})
    assert result.state.transitions == 30 and result.reply.resolution == "handoff"
    assert not result.reply.sources and not result.reply.items
    assert (
        router.route(
            replace(state, transitions=29), {"type": "button", "option_id": "a"}
        ).reply.resolution
        == "results"
    )
