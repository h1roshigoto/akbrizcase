"""DN-09 PostgreSQL 16 security & concurrency acceptance for grounded GigaChat."""

import secrets
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update
from sqlalchemy.pool import QueuePool
from test_catalog_applicability import Corpus, corpus
from test_chat_modes import edition
from test_chat_sessions import ORIGIN, create

from app.core.csrf import StaffSecurity
from app.core.db import Database
from app.main import app
from app.modules.catalog.models import CatalogItem
from app.modules.chat.diagnosis import HANDOFF_TEXT
from app.modules.chat.models import ChatSession
from app.modules.kb.models import DocumentVersion
from app.modules.llm import GroundedAnswer, GroundedRequest, LLMError, TokenUsage
from app.schemas.sources import SourceRef

pytestmark = pytest.mark.integration
URL = "/api/v1/chat/messages"


@pytest.fixture
def llm_db(databases: tuple[Database, Database]) -> Iterator[Database]:
    try:
        yield databases[0]
    finally:
        app.state.llm_client = None
        with databases[0].unit_of_work() as session:
            session.execute(delete(ChatSession))


@pytest.fixture
def llm_client_browser(llm_db: Database) -> Iterator[TestClient]:
    with TestClient(app, client=("192.0.2.1", 50000), base_url=ORIGIN) as client:
        app.state.database = llm_db
        app.state.staff_security = StaffSecurity(
            frozenset({ORIGIN}), secrets.token_hex(32)
        )
        yield client


def _headers(token: str) -> dict[str, str]:
    return {"X-Chat-Token": token}


def _post_text(
    client: TestClient,
    session_id: str,
    token: str,
    text_value: str,
    expected_version: int,
    request_id: UUID | None = None,
) -> Any:
    return client.post(
        URL,
        json={
            "session_id": session_id,
            "request_id": str(request_id or uuid4()),
            "expected_version": expected_version,
            "input": {"type": "text", "value": text_value},
        },
        headers=_headers(token),
    )


def _select_mode(
    client: TestClient, session_id: str, token: str, mode: str, expected_version: int
) -> Any:
    return client.post(
        URL,
        json={
            "session_id": session_id,
            "request_id": str(uuid4()),
            "expected_version": expected_version,
            "input": {"type": "select_mode", "mode": mode},
        },
        headers=_headers(token),
    )


def _make_stub_llm(fn: Any, model: str = "GigaChat-2") -> Any:
    return SimpleNamespace(
        settings=SimpleNamespace(model=model),
        generate_grounded=fn,
    )


def _failing_llm(
    code: str,
) -> Callable[[GroundedRequest], tuple[GroundedAnswer, TokenUsage]]:
    def _fail(_req: GroundedRequest) -> tuple[GroundedAnswer, TokenUsage]:
        raise LLMError(code)

    return _fail


def _seed_corpus(db: Database) -> tuple[Corpus, SourceRef, str, str]:
    needle = "syntheticdn09" + uuid4().hex
    with db.unit_of_work() as session:
        c = corpus(session)
        src = edition(
            session,
            c.actor,
            content=f"{needle}: тестовый фрагмент по датчику.",
        )
        item = session.get(CatalogItem, c.item)
        assert item is not None
        sku = item.sku
    return c, src, needle, sku


def test_grounded_knowledge_commerce_diagnose_and_zero_db_pool_held(
    llm_client_browser: TestClient,
    llm_db: Database,
) -> None:
    _, _, needle, sku_query = _seed_corpus(llm_db)
    checked_out_during_llm: list[int] = []
    llm_calls: list[str] = []

    def generate(req: GroundedRequest) -> tuple[GroundedAnswer, TokenUsage]:
        assert isinstance(llm_db.engine.pool, QueuePool)
        checked_out_during_llm.append(llm_db.engine.pool.checkedout())
        llm_calls.append(req.question)
        return (
            GroundedAnswer(
                answer=f"Ответ LLM по запросу: {req.question}",
                cited_section_ids=[f.section_id for f in req.fragments[:1]],
                cited_item_ids=[i.item_id for i in req.items[:1]],
                needs_handoff=False,
            ),
            TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
        )

    app.state.llm_client = _make_stub_llm(generate)

    # 1. Knowledge grounded happy path + idempotency replay
    created = create(llm_client_browser, mode="knowledge")
    sid, tok = created["session_id"], created["session_token"]
    req_id = uuid4()
    resp = _post_text(llm_client_browser, sid, tok, needle, 0, request_id=req_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"]["mode"] == "knowledge"
    assert body["reply"]["resolution"] == "results"
    assert body["reply"]["text"] == f"Ответ LLM по запросу: {needle}"
    assert len(body["reply"]["sources"]) == 1
    assert any(a["type"] == "open_document" for a in body["reply"]["actions"])
    assert checked_out_during_llm == [0]

    # Idempotency replay does NOT invoke LLM a second time
    replay = _post_text(llm_client_browser, sid, tok, needle, 0, request_id=req_id)
    assert replay.status_code == 200
    assert replay.json()["message_id"] == body["message_id"]
    assert len(llm_calls) == 1

    # 2. Commerce grounded happy path returns live catalog item + open_item action
    comm = create(llm_client_browser, mode="commerce")
    c_resp = _post_text(
        llm_client_browser, comm["session_id"], comm["session_token"], sku_query, 0
    )
    assert c_resp.status_code == 200
    c_body = c_resp.json()
    assert c_body["reply"]["mode"] == "commerce"
    assert c_body["reply"]["resolution"] == "results"
    assert len(c_body["reply"]["items"]) == 1
    assert any(a["type"] == "open_item" for a in c_body["reply"]["actions"])
    assert checked_out_during_llm == [0, 0]

    # 3. Diagnose mode NEVER calls LLM
    diag = create(llm_client_browser, mode="diagnose")
    d_resp = _post_text(
        llm_client_browser, diag["session_id"], diag["session_token"], needle, 0
    )
    assert d_resp.status_code == 200
    assert len(llm_calls) == 2


def test_revocation_during_phase2_and_after_commit_fails_closed(
    llm_client_browser: TestClient,
    llm_db: Database,
) -> None:
    c, src_inflight, needle_inflight, _ = _seed_corpus(llm_db)
    needle_committed = "syntheticdn09" + uuid4().hex
    with llm_db.unit_of_work() as db_session:
        src_committed = edition(
            db_session,
            c.actor,
            content=f"{needle_committed}: второй тестовый фрагмент.",
        )

    created = create(llm_client_browser, mode="knowledge")
    sid, tok = created["session_id"], created["session_token"]

    # 1. Revoke cited version WHILE LLM Phase 2 is in flight -> handoff in Phase 3
    def revoke_in_flight(req: GroundedRequest) -> tuple[GroundedAnswer, TokenUsage]:
        assert req.fragments
        with llm_db.unit_of_work() as db_session:
            db_session.execute(
                update(DocumentVersion)
                .where(DocumentVersion.id == src_inflight.document_version_id)
                .values(status="revoked")
            )
        return (
            GroundedAnswer(
                answer="Устаревший текст после отзыва",
                cited_section_ids=[req.fragments[0].section_id],
                cited_item_ids=[],
                needs_handoff=False,
            ),
            TokenUsage(total_tokens=12),
        )

    app.state.llm_client = _make_stub_llm(revoke_in_flight)
    r1 = _post_text(llm_client_browser, sid, tok, needle_inflight, 0)
    assert r1.status_code == 200
    assert r1.json()["reply"]["resolution"] == "handoff"
    assert r1.json()["reply"]["text"] == HANDOFF_TEXT

    # 2. Commit succeeds, then revoke cited version -> replay & history hide prose
    def ok_generate(req: GroundedRequest) -> tuple[GroundedAnswer, TokenUsage]:
        return (
            GroundedAnswer(
                answer="Сохранённый ответ по документу",
                cited_section_ids=[req.fragments[0].section_id],
                cited_item_ids=[],
                needs_handoff=False,
            ),
            TokenUsage(total_tokens=15),
        )

    app.state.llm_client = _make_stub_llm(ok_generate)
    req_id = uuid4()
    r2 = _post_text(
        llm_client_browser, sid, tok, needle_committed, 1, request_id=req_id
    )
    assert r2.status_code == 200
    assert r2.json()["reply"]["text"] == "Сохранённый ответ по документу"

    with llm_db.unit_of_work() as db_session:
        db_session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id == src_committed.document_version_id)
            .values(status="revoked")
        )
    replay = _post_text(
        llm_client_browser, sid, tok, needle_committed, 1, request_id=req_id
    )
    assert replay.status_code == 200
    assert replay.json()["reply"]["resolution"] == "handoff"
    assert replay.json()["reply"]["text"] == HANDOFF_TEXT

    hist = llm_client_browser.get(f"/api/v1/chat/sessions/{sid}", headers=_headers(tok))
    assert hist.status_code == 200
    latest_msg = hist.json()["messages"][0]
    assert latest_msg["reply"]["resolution"] == "handoff"
    assert latest_msg["reply"]["text"] == HANDOFF_TEXT


def test_version_race_and_provider_error_fallback(
    llm_client_browser: TestClient,
    llm_db: Database,
) -> None:
    _, _, needle, _ = _seed_corpus(llm_db)
    created = create(llm_client_browser, mode="knowledge")
    sid, tok = created["session_id"], created["session_token"]

    # 1. Version conflict race during Phase 2 -> 409 STATE_CONFLICT
    def race_during_llm(req: GroundedRequest) -> tuple[GroundedAnswer, TokenUsage]:
        # Concurrent request bumps version from 0 -> 1 before Phase 3 commits
        with ThreadPoolExecutor(max_workers=1) as pool:
            other = pool.submit(
                _select_mode, llm_client_browser, sid, tok, "commerce", 0
            ).result(timeout=10)
            assert other.status_code == 200
        return (
            GroundedAnswer(
                answer="Запоздалый ответ",
                cited_section_ids=[req.fragments[0].section_id],
                cited_item_ids=[],
                needs_handoff=False,
            ),
            TokenUsage(total_tokens=10),
        )

    app.state.llm_client = _make_stub_llm(race_during_llm)
    raced = _post_text(llm_client_browser, sid, tok, needle, 0)
    assert raced.status_code == 409
    assert raced.json()["error"]["code"] == "STATE_CONFLICT"

    # 2. Provider errors fall back cleanly to deterministic FTS results (HTTP 200)
    sw = _select_mode(llm_client_browser, sid, tok, "knowledge", 1)
    assert sw.status_code == 200
    for code in ("LLM_TIMEOUT", "LLM_RATE_LIMITED", "LLM_BUDGET_EXCEEDED"):
        app.state.llm_client = _make_stub_llm(_failing_llm(code))
        ver = llm_client_browser.get(
            f"/api/v1/chat/sessions/{sid}", headers=_headers(tok)
        ).json()["state_version"]
        fb = _post_text(llm_client_browser, sid, tok, needle, ver)
        assert fb.status_code == 200
        assert fb.json()["reply"]["resolution"] == "results"
        assert fb.json()["reply"]["text"].startswith(
            "Цитаты из опубликованных материалов"
        )
