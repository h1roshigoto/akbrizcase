"""MVP-035: public scenario menu and actual HTTP starts on PostgreSQL."""

import secrets
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, update
from test_catalog_applicability import corpus
from test_chat_messages import body, post
from test_chat_messages import browsers as browsers
from test_chat_messages import message_db as message_db
from test_chat_sessions import create
from test_tree_lifecycle import prepared, run

from app.core.csrf import SESSION_COOKIE
from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff_models import StaffSession
from app.core.staff_sessions import token_hash
from app.modules.catalog.models import EquipmentModel
from app.modules.kb.models import Document, DocumentVersion
from app.modules.kb.tree_models import DiagnosticTree
from app.schemas.chat import ChatScenarios

pytestmark = pytest.mark.integration
URL = "/api/v1/chat/scenarios"


def menu(browser: TestClient, model: UUID) -> list[dict[str, str]]:
    response = browser.get(URL, params={"model_id": str(model)})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    ChatScenarios.model_validate(response.json())
    assert set(response.json()) == {"items"}
    assert all(set(i) == {"key", "title", "model_id"} for i in response.json()["items"])
    result: list[dict[str, str]] = response.json()["items"]
    return result


def test_public_menu_start_and_disappearance(
    browsers: tuple[TestClient, TestClient], message_db: Database, tmp_path: Path
) -> None:
    browser, _ = browsers
    with message_db.unit_of_work() as session:
        c, tree_id, path, proof = prepared(session, tmp_path)
        tree = session.get(DiagnosticTree, tree_id)
        assert tree
        expected = {"key": tree.key, "title": tree.title, "model_id": str(c.model)}
        assert run(session, c, tree_id, "publish", path, proof).ok
    assert menu(browser, c.model) == [expected]
    own = create(browser, "diagnose")
    command = body(
        own,
        type="start_diagnosis",
        model_id=expected["model_id"],
        tree_key=expected["key"],
    )
    reply = post(browser, own, command)
    assert (
        reply.status_code == 200 and reply.json()["reply"]["resolution"] == "question"
    )
    option = next(
        b["input"]
        for b in reply.json()["reply"]["buttons"]
        if b["input"]["type"] == "button"
    )
    transition = post(browser, own, body(own, 1, **option))
    assert (
        transition.status_code == 200
        and transition.json()["reply"]["resolution"] == "results"
    )
    with message_db.unit_of_work() as session:
        assert run(session, c, tree_id, "revoke").ok
    assert menu(browser, c.model) == []
    new = create(browser, "diagnose")
    refused = post(browser, new, body(new, **command["input"]))
    assert refused.status_code == 200
    assert refused.json()["reply"]["resolution"] == "handoff"
    assert refused.json()["reply"]["sources"] == []


@pytest.mark.parametrize(
    "case",
    [
        "draft",
        "retired",
        "revoked",
        "internal",
        "source_revoked",
        "source_retired",
        "document_revoked",
        "model_draft",
        "model_archived",
        "unknown_model",
    ],
)
def test_hidden_candidates_and_staff_do_not_expand_access(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    tmp_path: Path,
    case: str,
) -> None:
    browser, _ = browsers
    with message_db.unit_of_work() as session:
        c, tree_id, path, proof = prepared(session, tmp_path)
        if case != "draft":
            assert run(session, c, tree_id, "publish", path, proof).ok
        if case in {"retired", "revoked"}:
            assert run(
                session, c, tree_id, "retire" if case == "retired" else "revoke"
            ).ok
        if case == "internal":
            session.execute(
                update(Document)
                .where(Document.id == c.document)
                .values(visibility="internal")
            )
        if case.startswith("source_"):
            session.execute(
                update(DocumentVersion)
                .where(DocumentVersion.id == c.version)
                .values(status=case.removeprefix("source_"))
            )
        if case == "document_revoked":
            session.execute(
                update(Document)
                .where(Document.id == c.document)
                .values(status="revoked")
            )
        if case.startswith("model_"):
            session.execute(
                update(EquipmentModel)
                .where(EquipmentModel.id == c.model)
                .values(status=case.removeprefix("model_"))
            )
        token = secrets.token_hex(32)
        session.add(
            StaffSession(
                user_id=c.actor,
                token_hash=token_hash(token),
                expires_at=utc_now() + timedelta(hours=1),
            )
        )
    model = uuid4() if case == "unknown_model" else c.model
    assert menu(browser, model) == []
    browser.cookies.set(SESSION_COOKIE, token)
    assert menu(browser, model) == []


def test_limit_after_acl_versions_and_no_writes(
    browsers: tuple[TestClient, TestClient], message_db: Database, tmp_path: Path
) -> None:
    browser, _ = browsers
    with message_db.unit_of_work() as session:
        public, first, path, proof = prepared(
            session, tmp_path, key="ZZZ-035-" + uuid4().hex
        )
        assert run(session, public, first, "publish", path, proof).ok
        hidden_source = corpus(session)
        hidden_context = replace(
            public,
            section=hidden_source.section,
            version=hidden_source.version,
            document=hidden_source.document,
        )
        _, hidden, path, proof = prepared(
            session, tmp_path, c=hidden_context, key="AAA-hidden-" + uuid4().hex
        )
        assert run(session, public, hidden, "publish", path, proof).ok
        session.execute(
            update(Document)
            .where(Document.id == hidden_source.document)
            .values(visibility="internal")
        )
        original = session.get(DiagnosticTree, first)
        assert original
        key = original.key
        _, second, path, proof = prepared(
            session, tmp_path, c=public, key=key, version=2
        )
        assert run(session, public, second, "publish", path, proof).ok
    assert len(menu(browser, public.model)) == 1
    with message_db.unit_of_work() as session:
        for i in range(11):
            _, tree, path, proof = prepared(
                session, tmp_path, c=public, key=f"M035-{i:02}-" + uuid4().hex
            )
            assert run(session, public, tree, "publish", path, proof).ok
    statements: list[str] = []

    def capture(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(message_db.engine, "before_cursor_execute", capture)
    try:
        items = menu(browser, public.model)
    finally:
        event.remove(message_db.engine, "before_cursor_execute", capture)
    assert len(items) == 10 and len({i["key"] for i in items}) == 10
    assert not {"INSERT", "UPDATE", "DELETE"} & set(statements)


def test_invalid_persisted_graph_never_becomes_a_public_choice(
    browsers: tuple[TestClient, TestClient], message_db: Database, tmp_path: Path
) -> None:
    browser, _ = browsers
    with message_db.unit_of_work() as session:
        c, _, _, _ = prepared(session, tmp_path)
        # Defense in depth against structurally corrupt persisted data. No
        # trigger or constraint is disabled: the real public loader rejects it.
        now = utc_now()
        session.add(
            DiagnosticTree(
                key="SYNTHETIC035-BROKEN-" + uuid4().hex,
                version=1,
                model_id=c.model,
                title="SYNTHETIC_PRIVATE_BROKEN_TITLE",
                graph={},
                yaml_checksum="a" * 64,
                git_commit="a" * 40,
                status="published",
                expert_name="SYNTHETIC ONLY",
                review_reference="SYNTHETIC:035",
                approved_at=now,
                published_at=now,
                published_by=c.actor,
            )
        )
    assert menu(browser, c.model) == []
