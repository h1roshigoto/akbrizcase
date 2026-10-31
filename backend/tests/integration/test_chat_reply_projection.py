"""MVP-034: synthetic PostgreSQL HTTP disclosure and transaction boundaries."""

from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_catalog_applicability import Corpus, confirmed, corpus
from test_catalog_applicability import pipeline as pipeline
from test_chat_messages import body, post, saved, scenario, start
from test_chat_messages import browsers as browsers
from test_chat_messages import message_db as message_db
from test_chat_modes import edition
from test_chat_sessions import create, read
from test_kb_import import PASSWORD
from test_kb_lifecycle import operation
from test_tree_lifecycle import prepared, run

from app.core.csrf import SESSION_COOKIE
from app.core.db import Database
from app.modules.catalog.models import CatalogItem, CatalogItemModel
from app.modules.chat.diagnosis import DiagnosisState
from app.modules.chat.handoff import GENERAL_SUPPORT_DRAFT
from app.modules.chat.message_state import ResponseEnvelope, StateAfter, decode_response
from app.modules.chat.mode_router import ModeRouter
from app.modules.chat.models import ChatMessage
from app.modules.chat.projection import ChatReplyProjector, begin_projection_read
from app.modules.kb.access import PUBLIC_VIEWER
from app.modules.kb.files import FilePipeline
from app.modules.kb.models import Document, DocumentVersion
from app.modules.kb.read_service import KnowledgeReadService
from app.schemas.sources import SourceRef

pytestmark = pytest.mark.integration


def knowledge(
    database: Database, browser: TestClient
) -> tuple[dict[str, Any], dict[str, Any], SourceRef, Corpus]:
    needle = "synthetic034" + uuid4().hex
    with database.unit_of_work() as session:
        c = corpus(session)
        source = edition(
            session, c.actor, content=f"{needle}: дословная тестовая цитата."
        )
    own = create(browser, "knowledge")
    request = body(own, type="text", value=needle)
    return own, request, source, c


def reply(response: Any, resolution: str) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    value: dict[str, Any] = response.json()["reply"]
    assert value["resolution"] == resolution
    assert response.headers["cache-control"] == "no-store"
    for private in ("state_after", "format_version", "transitions", "request_hash"):
        assert private not in response.text
    return value


def hidden(response: Any) -> None:
    value = reply(response, "handoff")
    assert value["sources"] == value["items"] == value["buttons"] == []
    assert [a["type"] for a in value["actions"]] == ["contact_specialist"]
    assert value["actions"][0]["payload"]["comment_draft"] == GENERAL_SUPPORT_DRAFT


def change_response(database: Database, own: dict[str, Any], value: Any) -> None:
    with database.unit_of_work() as session:
        session.execute(
            update(ChatMessage)
            .where(ChatMessage.session_id == UUID(own["session_id"]))
            .values(response=value)
        )


def test_exact_quotes_replay_own_modes_and_no_read_writes(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one, _ = browsers
    own, request, source, _ = knowledge(message_db, one)
    first = post(one, own, request)
    visible = reply(first, "results")
    with message_db.unit_of_work() as session:
        hit = KnowledgeReadService(session, PUBLIC_VIEWER).get_excerpt(
            source.section_id
        )
    assert visible["text"] == (
        "Цитаты из опубликованных материалов (не диагноз):\n\n[1] " + hit.excerpt
    )
    assert visible["sources"] == [source.model_dump(mode="json")]
    assert visible["actions"] == [
        {"type": "open_document", "payload": visible["sources"][0]}
    ]
    reply(post(one, own, body(own, 1, type="select_mode", mode="commerce")), "no_match")
    before, records = saved(message_db, own)
    original = [deepcopy(r.response) for r in records]

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Reading history/replay must not route or search")

    monkeypatch.setattr(ModeRouter, "route", forbidden)
    monkeypatch.setattr(KnowledgeReadService, "search", forbidden)
    statements = []

    def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(message_db.engine, "before_cursor_execute", capture)
    try:
        assert post(one, own, request).json() == first.json()
        history = read(one, own)
        reply(history, "no_match")
        messages = history.json()["messages"]
        assert [m["reply"]["mode"] for m in messages] == ["knowledge", "commerce"]
        assert messages[0]["reply"] == visible
        assert messages[0]["message_id"] == first.json()["message_id"]
    finally:
        event.remove(message_db.engine, "before_cursor_execute", capture)
    assert not ({"INSERT", "UPDATE", "DELETE"} & set(statements))
    after, records = saved(message_db, own)
    assert (after.state_version, after.outcome, after.last_activity_at) == (
        before.state_version,
        before.outcome,
        before.last_activity_at,
    )
    assert [r.response for r in records] == original


@pytest.mark.parametrize("change", ["document", "version", "internal"])
def test_source_loss_hides_new_replay_and_history_even_with_staff_cookie(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    change: str,
) -> None:
    one, staff = browsers
    own, request, source, c = knowledge(message_db, one)
    first = post(one, own, request)
    reply(first, "results")
    with message_db.unit_of_work() as session:
        if change == "version":
            session.execute(
                update(DocumentVersion)
                .where(DocumentVersion.id == source.document_version_id)
                .values(status="revoked")
            )
        else:
            session.execute(
                update(Document)
                .where(Document.id == source.document_id)
                .values(
                    **(
                        {"visibility": "internal"}
                        if change == "internal"
                        else {"status": "revoked"}
                    )
                )
            )
    csrf = staff.get("/admin/auth/csrf").json()["csrf_token"]
    assert (
        staff.post(
            "/admin/auth/login",
            json={"login": c.login, "password": PASSWORD},
            headers={"Origin": "https://localhost:8443", "X-CSRF-Token": csrf},
        ).status_code
        == 200
    )
    assert SESSION_COOKIE in staff.cookies
    for browser in (one, staff):
        replay = post(browser, own, request)
        hidden(replay)
        assert replay.json()["message_id"] == first.json()["message_id"]
        assert replay.json()["state_version"] == 1
        history = read(browser, own)
        hidden(history)
        assert history.json()["messages"][0]["reply"] == history.json()["reply"]
        assert str(source.section_id) not in history.text
    for browser in (one, staff):
        fresh = post(
            browser,
            own,
            body(
                own,
                saved(message_db, own)[0].state_version,
                type="text",
                value=request["input"]["value"],
            ),
        )
        reply(fresh, "no_match")


def test_retired_exact_edition_metadata_and_published_only_new_search(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
) -> None:
    own, request, source, _ = knowledge(message_db, browsers[0])
    first = post(browsers[0], own, request)
    reply(first, "results")
    with message_db.unit_of_work() as session:
        session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id == source.document_version_id)
            .values(status="retired")
        )
        session.execute(
            update(Document)
            .where(Document.id == source.document_id)
            .values(title="SYNTHETIC refreshed title")
        )
    for response in (post(browsers[0], own, request), read(browsers[0], own)):
        value = reply(response, "results")
        current = value["sources"][0]
        assert current["title"] == "SYNTHETIC refreshed title (предыдущая редакция)"
        assert current["document_version_id"] == str(source.document_version_id)
        assert current["url"] == source.url
        assert value["actions"][0]["payload"] == current
    reply(post(browsers[0], own, body(own, 1, **request["input"])), "no_match")


@pytest.mark.parametrize(
    "damage",
    ["text", "source_identity", "action_extra", "action_url", "button", "foreign"],
)
def test_untrusted_jsonb_is_never_authority(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    damage: str,
) -> None:
    own, request, source, _ = knowledge(message_db, browsers[0])
    reply(post(browsers[0], own, request), "results")
    raw: Any = deepcopy(saved(message_db, own)[1][0].response)
    if damage == "text":
        raw["reply"]["text"] += " SYNTHETIC_UNAPPROVED_TECHNICAL_TEXT"
    elif damage == "source_identity":
        raw["reply"]["sources"][0]["document_id"] = str(uuid4())
    elif damage == "action_extra":
        raw["reply"]["actions"][0]["payload"]["execute"] = "SYNTHETIC_POISON"
    elif damage == "action_url":
        raw["reply"]["actions"][0]["payload"]["url"] = "https://example.invalid/poison"
    elif damage == "button":
        raw["reply"]["buttons"][0]["input"]["next"] = "SYNTHETIC_POISON"
    else:
        raw = {"SYNTHETIC_POISON": "private envelope"}
    change_response(message_db, own, raw)
    for response in (post(browsers[0], own, request), read(browsers[0], own)):
        hidden(response)
        assert "SYNTHETIC_POISON" not in response.text
        assert str(source.section_id) not in response.text


def test_valid_but_forged_actions_and_labels_are_rebuilt(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
) -> None:
    own, request, source, c = knowledge(message_db, browsers[0])
    original = reply(post(browsers[0], own, request), "results")
    raw: Any = deepcopy(saved(message_db, own)[1][0].response)
    raw["reply"]["actions"] = [
        {"type": "open_item", "payload": {"item_id": str(c.item)}},
        {
            "type": "contact_specialist",
            "payload": {"kind": "support", "comment_draft": "SYNTHETIC_POISON"},
        },
    ]
    raw["reply"]["buttons"][0]["label"] = "SYNTHETIC_POISON"
    raw["reply"]["sources"][0]["title"] = "SYNTHETIC_POISON"
    change_response(message_db, own, raw)
    assert reply(post(browsers[0], own, request), "results") == original
    assert source.model_dump(mode="json") in original["sources"]


def test_catalog_refresh_archive_and_text_actions_are_consistent(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
) -> None:
    one, _ = browsers
    with message_db.unit_of_work() as session:
        c = corpus(session)
        item = session.get(CatalogItem, c.item)
        assert item
        item.name = "synthetic034" + uuid4().hex
        item.price_mode, item.price, item.image_key = (
            "fixed",
            Decimal("123.45"),
            "a" * 64 + ".png",
        )
        term = item.sku
    own = create(one, "commerce")
    request = body(own, type="text", value=term)
    first = post(one, own, request)
    original = reply(first, "results")["items"][0]
    assert original["price"] == "123.45" and original["applicability"] is None
    with message_db.unit_of_work() as session:
        item = session.get(CatalogItem, c.item)
        assert item
        item.price = Decimal("234.56")
        item.revision += 1
        item.image_key = "b" * 64 + ".png"
        item.name = "SYNTHETIC renamed"
    for response in (post(one, own, request), read(one, own)):
        current = reply(response, "results")
        card = current["items"][0]
        assert (
            card["price"] == "234.56" and card["revision"] == original["revision"] + 1
        )
        assert card["image_url"].endswith("b" * 64 + ".png")
        assert card["name"] == "SYNTHETIC renamed"
        assert "123.45" not in response.text
        form = next(a for a in current["actions"] if a["type"] == "open_request_form")
        assert form["payload"]["items"] == [{"item_id": str(c.item), "qty": 1}]
    with message_db.unit_of_work() as session:
        session.execute(
            update(CatalogItem)
            .where(CatalogItem.id == c.item)
            .values(status="archived")
        )
    for response in (post(one, own, request), read(one, own)):
        current = reply(response, "no_match")
        assert current["items"] == []
        assert str(c.item) not in response.text
        assert [a["type"] for a in current["actions"]] == ["contact_specialist"]
        assert "специалисту" in current["text"]


def test_pinned_retired_tree_source_and_current_state_button_checks(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    tmp_path: Path,
) -> None:
    one, _ = browsers
    c, first, key = scenario(message_db, tmp_path)
    own = create(one)
    request = start(own, c, key)
    original = reply(post(one, own, request), "question")
    assert original["text"] == "SYNTHETIC choice"
    old_button = next(
        b["input"] for b in original["buttons"] if b["input"]["type"] == "button"
    )
    with message_db.unit_of_work() as session:
        _, second, path, proof = prepared(session, tmp_path, c=c, key=key, version=2)
        assert run(session, c, second, "publish", path, proof).ok
        session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id == c.version)
            .values(status="retired")
        )
    history = read(one, own)
    current = reply(history, "question")
    assert current["text"] == original["text"]
    assert current["sources"][0]["title"].endswith("(предыдущая редакция)")
    assert saved(message_db, own)[0].tree_id == first
    # Old option is always checked against the current node in POST.
    result_request = body(own, 1, **old_button)
    result = reply(post(one, own, result_request), "results")
    assert result["items"] == []  # retired evidence no longer confirms applicability
    assert not any(
        a["type"] in {"open_item", "open_request_form"} for a in result["actions"]
    )
    assert reply(post(one, own, body(own, 2, **old_button)), "results") == result
    assert saved(message_db, own)[0].current_node_key == "result"
    with message_db.unit_of_work() as session:
        assert run(session, c, first, "revoke").ok
    hidden(post(one, own, request))
    history = read(one, own)
    hidden(history)
    assert all(
        m["reply"]["resolution"] == "handoff" for m in history.json()["messages"]
    )
    assert str(c.section) not in history.text


@pytest.mark.parametrize("loss", ["applicability", "archive"])
def test_diagnostic_recommendation_loss_preserves_allowed_technical_reply(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    tmp_path: Path,
    loss: str,
) -> None:
    one, _ = browsers
    c, _, key = scenario(message_db, tmp_path)
    own = create(one)
    reply(post(one, own, start(own, c, key)), "question")
    request = body(own, 1, type="button", option_id="a")
    result = reply(post(one, own, request), "results")
    assert result["items"][0]["id"] == str(c.item)
    form = next(a for a in result["actions"] if a["type"] == "open_request_form")
    assert form["payload"]["items"] == [{"item_id": str(c.item), "qty": 2}]
    with message_db.unit_of_work() as session:
        if loss == "archive":
            session.execute(
                update(CatalogItem)
                .where(CatalogItem.id == c.item)
                .values(status="archived")
            )
        else:
            session.execute(
                update(CatalogItemModel)
                .where(CatalogItemModel.item_id == c.item)
                .values(public_allowed=False, published_at=None, published_by=None)
            )
    for response in (post(one, own, request), read(one, own)):
        current = reply(response, "results")
        assert (
            current["text"] == result["text"]
            and current["sources"] == result["sources"]
        )
        assert current["items"] == []
        assert str(c.item) not in response.text
        assert not any(
            a["type"] in {"open_item", "open_request_form"} for a in current["actions"]
        )


def test_after_commit_revocation_is_seen_before_post_disclosure(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one, _ = browsers
    own, request, source, _ = knowledge(message_db, one)
    from app.modules.chat.projection import begin_projection_read

    begin = begin_projection_read
    calls = 0

    def revoke_then_read(session: Session) -> None:
        nonlocal calls
        calls += 1
        # A separate connection sees the message only after the writer COMMIT.
        assert len(saved(message_db, own)[1]) == 1
        with message_db.unit_of_work() as writer:
            writer.execute(
                update(DocumentVersion)
                .where(DocumentVersion.id == source.document_version_id)
                .values(status="revoked")
            )
        begin(session)

    monkeypatch.setattr(
        "app.modules.chat.router.begin_projection_read", revoke_then_read
    )
    hidden(post(one, own, request))
    assert calls == 1
    row, records = saved(message_db, own)
    assert row.state_version == len(records) == 1
    assert decode_response(records[0]).reply.resolution == "results"


def test_snapshot_is_coherent_during_concurrent_revocation_and_next_read_hides(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one, _ = browsers
    own, request, source, _ = knowledge(message_db, one)
    original = reply(post(one, own, request), "results")
    excerpt = KnowledgeReadService.get_excerpt
    changed = False

    def interleaved(self: KnowledgeReadService, section_id: UUID) -> Any:
        nonlocal changed
        hit = excerpt(self, section_id)
        if not changed:
            changed = True
            with message_db.unit_of_work() as writer:
                writer.execute(
                    update(DocumentVersion)
                    .where(DocumentVersion.id == source.document_version_id)
                    .values(status="revoked")
                )
        return hit

    monkeypatch.setattr(KnowledgeReadService, "get_excerpt", interleaved)
    # Revoke completes during projection without content locks. The in-flight
    # read linearizes at its snapshot, including the subsequent action check.
    assert reply(read(one, own), "results") == original
    hidden(read(one, own))
    hidden(post(one, own, request))


def test_database_projection_failure_remains_503_and_retry_is_replay(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own, request, _, _ = knowledge(message_db, browsers[0])
    project = ChatReplyProjector.project

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise SQLAlchemyError("SYNTHETIC_PRIVATE_DATABASE_DIAGNOSTIC")

    monkeypatch.setattr(ChatReplyProjector, "project", unavailable)
    response = post(browsers[0], own, request)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "SYNTHETIC_PRIVATE" not in response.text
    assert len(saved(message_db, own)[1]) == 1  # committed, safe to retry same key
    assert read(browsers[0], own).status_code == 503
    monkeypatch.setattr(ChatReplyProjector, "project", project)
    reply(post(browsers[0], own, request), "results")
    assert len(saved(message_db, own)[1]) == 1


def test_server_model_context_rechecks_internal_evidence_and_republication(
    message_db: Database,
    tmp_path: Path,
    pipeline: FilePipeline,
) -> None:
    with message_db.unit_of_work() as session:
        c = corpus(session, visibility="internal")
        confirmed(session, c, tmp_path)
        item = session.get(CatalogItem, c.item)
        assert item
        logical = ModeRouter(session).route(
            DiagnosisState(mode="commerce"),
            {"type": "text", "value": item.sku},
            model_id=c.model,
        )
        envelope = ResponseEnvelope(
            format_version=1,
            reply=logical.reply,
            state_after=StateAfter(**asdict(logical.state), state_version=1),
        )

    def project() -> Any:
        with message_db.unit_of_work() as session:
            begin_projection_read(session)
            return ChatReplyProjector(session).project(envelope, model_id=c.model)

    first = project()
    assert first.items[0].applicability.status == "confirmed"
    assert first.items[0].applicability.sources == []
    for private in (str(c.section), str(c.document), "INTERNAL-SYNTHETIC"):
        assert private not in first.model_dump_json()
    with message_db.unit_of_work() as session:
        assert operation(session, c.login, pipeline, "retire", c.document, c.version).ok
    assert project().items[0].applicability.status == "unknown"
    with message_db.unit_of_work() as session:
        session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id == c.version)
            .values(status="published")
        )
    current = project()
    assert current.items[0].applicability.status == "unknown"
    assert current.items[0].applicability.sources == []
    # Without authoritative context, forged stored applicability grants nothing.
    with message_db.unit_of_work() as session:
        begin_projection_read(session)
        no_context = ChatReplyProjector(session).project(envelope)
    assert no_context.items[0].applicability is None


@pytest.mark.parametrize(
    "status,allowed", [("draft", True), ("published", False), ("revoked", True)]
)
def test_exact_technical_source_denied_by_current_acl(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    status: str,
    allowed: bool,
) -> None:
    own, request, _, c = knowledge(message_db, browsers[0])
    reply(post(browsers[0], own, request), "results")
    raw: Any = deepcopy(saved(message_db, own)[1][0].response)
    with message_db.unit_of_work() as session:
        denied = edition(session, c.actor, status=status, allowed=allowed)
    raw["reply"]["sources"] = [denied.model_dump(mode="json")]
    # Even a well-formed envelope with a permitted-looking exact ID cannot
    # authorize a draft, revoked or assistant-disallowed source.
    change_response(message_db, own, raw)
    hidden(post(browsers[0], own, request))
    hidden(read(browsers[0], own))


def test_valid_mixed_history_paginates_with_current_reply_independent_of_cursor(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
) -> None:
    own = create(browsers[0])
    expected = []
    modes = ["knowledge", "commerce", "diagnose"]
    for version in range(55):
        if version == 30:
            # Preserve the 55-message pagination regression across two quota
            # windows; production still accepts only 30 committed exchanges/min.
            with message_db.unit_of_work() as session:
                session.execute(
                    text(
                        "UPDATE app.rate_limit_buckets SET "
                        "started_at=started_at-interval '2 minutes', "
                        "expires_at=expires_at-interval '2 minutes' "
                        "WHERE scope='message'"
                    )
                )
        request = body(own, version, type="select_mode", mode=modes[version % 3])
        response = post(browsers[0], own, request)
        value = reply(response, "no_match")
        expected.append((response.json()["message_id"], value["mode"], version + 1))
    latest = expected[-1]
    seen: list[tuple[str, str, int]] = []
    params: dict[str, str] = {}
    while True:
        response = read(browsers[0], own, params=params)
        current = reply(response, "no_match")
        page = response.json()
        assert current["mode"] == latest[1] and page["state_version"] == 55
        assert len(page["messages"]) <= 50
        seen = [
            (m["message_id"], m["reply"]["mode"], m["state_version"])
            for m in page["messages"]
        ] + seen
        if page["next_before"] is None:
            break
        assert len(page["messages"]) == 50
        params = {"before": page["next_before"]}
    assert seen == expected
    assert saved(message_db, own)[0].state_version == 55


def test_new_tree_publication_during_projection_does_not_replace_pin(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.kb.tree_reads import load_tree

    one, _ = browsers
    c, pinned, key = scenario(message_db, tmp_path)
    own = create(one)
    request = start(own, c, key)
    first = reply(post(one, own, request), "question")
    changed = False

    def publish_during_read(session: Session, **kwargs: Any) -> Any:
        nonlocal changed
        snapshot = load_tree(session, **kwargs)
        assert snapshot and snapshot.tree_id == pinned
        if not changed:
            changed = True
            with message_db.unit_of_work() as writer:
                _, second, path, proof = prepared(
                    writer, tmp_path, c=c, key=key, version=2
                )
                assert run(writer, c, second, "publish", path, proof).ok
        return snapshot

    monkeypatch.setattr("app.modules.chat.projection.load_tree", publish_during_read)
    assert reply(read(one, own), "question") == first
    assert reply(post(one, own, request), "question") == first
    assert saved(message_db, own)[0].tree_id == pinned


def test_exact_source_metadata_does_not_require_nonempty_excerpt(
    message_db: Database,
) -> None:
    with message_db.unit_of_work() as session:
        c = corpus(session)
        source = edition(session, c.actor, content="")
        reader = KnowledgeReadService(session, PUBLIC_VIEWER)
        # Preserve the pre-existing SourceRef-only contract. An empty excerpt
        # cannot become a knowledge quote, but does not erase valid metadata.
        assert reader.get_source(source.section_id) == source
        with pytest.raises(ValidationError):
            reader.get_excerpt(source.section_id)


def test_privacy_shutdown_replay_rechecks_revoked_source(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    databases: tuple[Database, Database],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_privacy import packet

    from app.core.db_types import utc_now
    from app.modules.orders.models import ConsentText
    from app.modules.orders.privacy_operator import run_privacy

    try:
        one, _ = browsers
        own, request, source, c = knowledge(message_db, one)
        first = post(one, own, request)
        assert first.status_code == 200 and first.json()["reply"]["sources"]
        path, bundle = packet(tmp_path, approved=True)
        with databases[1].unit_of_work() as session:
            session.execute(update(ConsentText).values(retired_at=utc_now()))
            assert run_privacy(
                session,
                login=c.login,
                password=PASSWORD,
                operation="publish",
                expected_version=None,
                path=path,
                expected_checksum=bundle.checksum(),
            ).ok
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("PRIVACY_CONFIG_FILE", raising=False)
        with databases[1].unit_of_work() as session:
            assert run_privacy(
                session,
                login=c.login,
                password=PASSWORD,
                operation="retire",
                expected_version=bundle.consent.version,
            ).ok
            session.execute(
                update(Document)
                .where(Document.id == source.document_id)
                .values(visibility="internal")
            )
        before, records = saved(message_db, own)
        monkeypatch.setattr(
            ModeRouter, "route", lambda *args: pytest.fail("Replay cannot reroute")
        )
        replay = post(one, own, request)
        hidden(replay)
        assert replay.json()["message_id"] == first.json()["message_id"]
        assert replay.json()["state_version"] == first.json()["state_version"]
        assert str(source.section_id) not in replay.text
        after, same = saved(message_db, own)
        assert (
            after.state_version == before.state_version
            and len(same) == len(records) == 1
        )
    finally:
        # The suite owns a fresh synthetic DB. Do not leak this deliberately
        # retired publication into subsequent demo order fixtures.
        with databases[1].unit_of_work() as session:
            session.execute(text("TRUNCATE app.consent_publications CASCADE"))
