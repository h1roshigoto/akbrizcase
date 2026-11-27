"""DN-10 PostgreSQL 16 integration acceptance for DB context resolver and runner."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.pool import QueuePool
from test_catalog_applicability import corpus
from test_chat_modes import edition

from app.core.db import Database
from app.modules.catalog.models import CatalogItem
from app.modules.llm import eval_cli
from app.modules.llm.budget import BudgetLedger, PostgresBudgetLedger
from app.modules.llm.config import LLMSettings
from app.modules.llm.eval_runner import (
    AcceptanceThresholds,
    EvalCase,
    EvalDataset,
    resolve_context_from_db,
    run_evaluation,
)
from app.modules.llm.schemas import (
    GroundedAnswer,
    GroundedCatalogItem,
    GroundedFragment,
    GroundedRequest,
    TokenUsage,
)

pytestmark = pytest.mark.integration


def test_eval_db_context_enforces_public_acl_and_releases_pool(
    databases: tuple[Database, Database],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = databases[0]
    needle = "syntheticdn10" + uuid4().hex

    with db.unit_of_work() as session:
        c = corpus(session)
        catalog_item = session.get(CatalogItem, c.item)
        assert catalog_item is not None
        sku = catalog_item.sku

        public_sec = edition(
            session,
            c.actor,
            content=f"{needle}: разрешённый опубликованный раздел.",
            visibility="public",
            status="published",
            allowed=True,
        )
        edition(
            session,
            c.actor,
            content=f"{needle}: закрытый внутренний раздел.",
            visibility="internal",
            status="published",
            allowed=True,
        )
        edition(
            session,
            c.actor,
            content=f"{needle}: отозванный раздел.",
            visibility="public",
            status="revoked",
            allowed=True,
        )
        edition(
            session,
            c.actor,
            content=f"{needle}: черновик.",
            visibility="public",
            status="draft",
            allowed=True,
        )
        edition(
            session,
            c.actor,
            content=f"{needle}: запрещённый для ассистента.",
            visibility="public",
            status="published",
            allowed=False,
        )

        kb_frags, kb_items = resolve_context_from_db(
            session,
            EvalCase(case_id="acl-kb", mode="knowledge", question=needle),
        )
        assert [f.section_id for f in kb_frags] == [public_sec.section_id]
        assert kb_items == []

        _, comm_items = resolve_context_from_db(
            session,
            EvalCase(case_id="acl-comm", mode="commerce", question=sku),
        )
        assert [i.item_id for i in comm_items] == [c.item]

    dataset = EvalDataset(
        version="pg16-dn10",
        synthetic=True,
        cases=[
            EvalCase(
                case_id="pg-kb-crit",
                mode="knowledge",
                critical=True,
                question=needle,
                expected_section_ids=[public_sec.section_id],
            ),
            EvalCase(
                case_id="pg-comm-reg",
                mode="commerce",
                critical=False,
                question=sku,
                expected_item_ids=[c.item],
            ),
            EvalCase(
                case_id="pg-oob",
                mode="knowledge",
                critical=False,
                expect_handoff=True,
                question="absentterm" + uuid4().hex,
            ),
        ],
    )

    checked_out: list[int] = []

    def make_client(model_name: str) -> Any:
        def generate(req: GroundedRequest) -> tuple[GroundedAnswer, TokenUsage]:
            assert isinstance(db.engine.pool, QueuePool)
            checked_out.append(db.engine.pool.checkedout())
            if req.items:
                return (
                    GroundedAnswer(
                        answer=f"Товар {req.items[0].sku} найден.",
                        cited_section_ids=[],
                        cited_item_ids=[req.items[0].item_id],
                        needs_handoff=False,
                    ),
                    TokenUsage(prompt_tokens=40, completion_tokens=20, total_tokens=60),
                )
            return (
                GroundedAnswer(
                    answer="Опубликованный фрагмент подтверждён.",
                    cited_section_ids=[req.fragments[0].section_id],
                    cited_item_ids=[],
                    needs_handoff=False,
                ),
                TokenUsage(prompt_tokens=30, completion_tokens=15, total_tokens=45),
            )

        return SimpleNamespace(
            settings=SimpleNamespace(model=model_name),
            generate_grounded=generate,
        )

    def resolve_short_uow(
        case: EvalCase,
    ) -> tuple[list[GroundedFragment], list[GroundedCatalogItem]]:
        with db.unit_of_work() as read_session:
            return resolve_context_from_db(read_session, case)

    report = run_evaluation(
        dataset,
        [make_client("GigaChat-2"), make_client("GigaChat-2-Pro")],
        context_resolver=resolve_short_uow,
        critical_repeats=3,
        thresholds=AcceptanceThresholds(min_models_compared=2),
    )
    assert report.overall_accepted is True
    assert report.accepted_models == ["GigaChat-2", "GigaChat-2-Pro"]
    # 2 models * (3 repeats on pg-kb-crit + 1 run on pg-comm-reg); pg-oob skips LLM
    assert checked_out == [0] * 8

    ledgers: list[BudgetLedger] = []

    def cli_client(settings: LLMSettings, *, ledger: BudgetLedger) -> Any:
        ledgers.append(ledger)
        return make_client(settings.model)

    source = tmp_path / "synthetic-cases.json"
    source.write_text(dataset.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(eval_cli, "GigaChatClient", cli_client)
    monkeypatch.setattr(
        LLMSettings, "from_environment", lambda: LLMSettings(auth_key="synthetic")
    )
    assert (
        eval_cli.main(
            [
                "--dataset",
                str(source),
                "--models",
                "GigaChat-2",
                "GigaChat-2-Pro",
                "--use-db",
            ]
        )
        == 0
    )
    assert isinstance(ledgers[0], PostgresBudgetLedger)
    assert ledgers[0] is ledgers[1]
