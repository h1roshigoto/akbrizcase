"""Unit tests for grounded LLM envelope, citation bounds, and tamper projection."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

from app.modules.chat.diagnosis import HANDOFF_TEXT, DiagnosisState
from app.modules.chat.grounded_chat import PreparedGroundedContext, run_grounded_llm
from app.modules.chat.message_state import (
    GroundedMeta,
    ResponseEnvelope,
    StateAfter,
    grounded_digest,
)
from app.modules.chat.projection import ChatReplyProjector
from app.modules.llm import GroundedAnswer, LLMError, TokenUsage
from app.modules.llm.schemas import GroundedCatalogItem, GroundedFragment
from app.schemas.chat import ChatReply
from app.schemas.sources import SourceRef


def _fake_client(answer: GroundedAnswer | Exception, model: str = "GigaChat-2") -> Any:
    def generate(req: object) -> tuple[GroundedAnswer, TokenUsage]:
        if isinstance(answer, Exception):
            raise answer
        return answer, TokenUsage(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        )

    return SimpleNamespace(
        settings=SimpleNamespace(model=model),
        generate_grounded=generate,
    )


def test_grounded_digest_and_run_grounded_llm_bounds() -> None:
    sec_id = uuid4()
    item_id = uuid4()
    other_id = uuid4()
    ctx = PreparedGroundedContext(
        mode="knowledge",
        question="Как подключить датчик?",
        fragments=[GroundedFragment(section_id=sec_id, title="Doc", text="Текст")],
        items=[
            GroundedCatalogItem(
                item_id=item_id,
                sku="SKU-1",
                name="Датчик",
                price_label="4500.00 RUB",
            )
        ],
        allowed_section_ids=frozenset({sec_id}),
        allowed_item_ids=frozenset({item_id}),
    )

    # 1. Valid grounded response
    ok = run_grounded_llm(
        _fake_client(
            GroundedAnswer(
                answer="Подключите по инструкции.",
                cited_section_ids=[sec_id],
                cited_item_ids=[],
                needs_handoff=False,
            )
        ),
        ctx,
    )
    assert ok is not None and ok.cited_section_ids == [sec_id]

    # 2. Provider LLMError falls back to None
    assert run_grounded_llm(_fake_client(LLMError("LLM_TIMEOUT")), ctx) is None

    # 3. Model handoff passes through
    handoff = run_grounded_llm(
        _fake_client(
            GroundedAnswer(
                answer="Недостаточно данных",
                cited_section_ids=[],
                cited_item_ids=[],
                needs_handoff=True,
            )
        ),
        ctx,
    )
    assert handoff is not None and handoff.needs_handoff is True

    # 4. Invented section_id / item_id / uncited knowledge rejected
    for bad_answer in (
        GroundedAnswer(
            answer="Поддельный источник",
            cited_section_ids=[other_id],
            cited_item_ids=[],
            needs_handoff=False,
        ),
        GroundedAnswer(
            answer="Поддельный товар",
            cited_section_ids=[sec_id],
            cited_item_ids=[other_id],
            needs_handoff=False,
        ),
        GroundedAnswer(
            answer="Ответ без источника",
            cited_section_ids=[],
            cited_item_ids=[],
            needs_handoff=False,
        ),
    ):
        assert run_grounded_llm(_fake_client(bad_answer), ctx) is None


def test_projector_rejects_tampered_grounded_envelope() -> None:
    sec_id = uuid4()
    doc_id = uuid4()
    ver_id = uuid4()
    src = SourceRef(
        section_id=sec_id,
        document_id=doc_id,
        document_version_id=ver_id,
        title="РЭ",
        section="Раздел 1",
        page_from=1,
        page_to=2,
        url=f"/api/v1/documents/{doc_id}/versions/{ver_id}/file",
    )
    orig_text = "Проверенный ответ"
    digest = grounded_digest("knowledge", orig_text, [sec_id], [])
    tampered_reply = ChatReply(
        mode="knowledge",
        text="Подменённый текст",
        buttons=[],
        sources=[src],
        items=[],
        actions=[],
        resolution="results",
    )
    state = DiagnosisState(mode="knowledge")
    envelope = ResponseEnvelope(
        format_version=1,
        reply=tampered_reply,
        state_after=StateAfter(
            mode=state.mode,
            tree_id=None,
            current_node_key=None,
            outcome=None,
            transitions=0,
            state_version=1,
        ),
        llm_grounded=GroundedMeta(model="GigaChat-2", content_digest=digest),
    )

    session = MagicMock()
    projector = ChatReplyProjector(session)
    projected = projector.project(envelope)
    assert projected.resolution == "handoff"
    assert projected.text == HANDOFF_TEXT
