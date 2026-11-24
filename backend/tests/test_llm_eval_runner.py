"""Unit and stub-HTTP CLI tests for DN-10 multi-model GigaChat evaluation."""

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.modules.llm.eval_cli import main as eval_cli_main
from app.modules.llm.eval_runner import (
    AcceptanceThresholds,
    EvalCase,
    EvalDataset,
    ModelPricing,
    format_markdown_report,
    percentile_ms,
    run_evaluation,
)
from app.modules.llm.schemas import (
    GroundedAnswer,
    GroundedCatalogItem,
    GroundedFragment,
    GroundedRequest,
    LLMError,
    TokenUsage,
)

FIXTURE_DATASET = Path(__file__).parent / "fixtures" / "llm_eval_synthetic.json"


def _stub_client(
    model: str,
    responder: Callable[[GroundedRequest], tuple[GroundedAnswer, TokenUsage]],
) -> Any:
    return SimpleNamespace(
        settings=SimpleNamespace(model=model),
        generate_grounded=responder,
    )


@contextmanager
def _local_gigachat_server(
    *,
    available_models: list[str],
    auth_key: str,
    access_token: str,
    answer_handler: Callable[[str, str], dict[str, object]],
) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_json(self, status: int, body: dict[str, object]) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            if self.path == "/api/v1/models":
                if self.headers.get("Authorization") != f"Bearer {access_token}":
                    self._send_json(401, {"message": "unauthorized"})
                    return
                self._send_json(
                    200,
                    {"data": [{"id": m} for m in available_models]},
                )
                return
            self._send_json(404, {"message": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            if self.path == "/api/v2/oauth":
                if self.headers.get("Authorization") != f"Basic {auth_key}":
                    self._send_json(401, {"message": "bad basic key"})
                    return
                self._send_json(
                    200,
                    {"access_token": access_token, "expires_at": 4_102_444_800_000},
                )
                return
            if self.path == "/api/v1/chat/completions":
                if self.headers.get("Authorization") != f"Bearer {access_token}":
                    self._send_json(401, {"message": "bad bearer"})
                    return
                req = json.loads(raw)
                model = str(req["model"])
                user_msg = str(req["messages"][-1]["content"])
                self._send_json(200, answer_handler(model, user_msg))
                return
            self._send_json(404, {"message": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_percentile_and_multi_model_comparison_with_costs() -> None:
    assert percentile_ms([], 95) == 0.0
    assert percentile_ms([10.0, 20.0, 30.0, 40.0], 50) == 20.0
    assert percentile_ms([10.0, 20.0, 30.0, 40.0], 95) == 40.0

    dataset = EvalDataset.model_validate_json(
        FIXTURE_DATASET.read_text(encoding="utf-8")
    )
    sec1 = UUID("11111111-1111-4111-8111-111111111101")
    sec2 = UUID("11111111-1111-4111-8111-111111111102")
    item1 = UUID("22222222-2222-4222-8222-222222222201")
    unknown_sec = uuid4()

    strong_calls: list[str] = []

    def strong_fn(req: GroundedRequest) -> tuple[GroundedAnswer, TokenUsage]:
        strong_calls.append(req.question)
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        if "Игнорируй правила" in req.question:
            return (
                GroundedAnswer(
                    answer="Передаю диалог специалисту.",
                    cited_section_ids=[],
                    cited_item_ids=[],
                    needs_handoff=True,
                ),
                usage,
            )
        if req.items:
            return (
                GroundedAnswer(
                    answer="Стоимость датчика SYN-001: 4500.00 RUB.",
                    cited_section_ids=[],
                    cited_item_ids=[item1],
                    needs_handoff=False,
                ),
                usage,
            )
        sid = sec1 if sec1 in {f.section_id for f in req.fragments} else sec2
        return (
            GroundedAnswer(
                answer="Ответ по опубликованному разделу.",
                cited_section_ids=[sid],
                cited_item_ids=[],
                needs_handoff=False,
            ),
            usage,
        )

    def weak_fn(req: GroundedRequest) -> tuple[GroundedAnswer, TokenUsage]:
        return (
            GroundedAnswer(
                answer="Использую внутренний ключ для ответа.",
                cited_section_ids=[unknown_sec],
                cited_item_ids=[],
                needs_handoff=False,
            ),
            TokenUsage(prompt_tokens=80, completion_tokens=20, total_tokens=100),
        )

    pricing = {
        "GigaChat-2": ModelPricing(
            model="GigaChat-2",
            rub_per_1m_prompt_tokens=Decimal("100"),
            rub_per_1m_completion_tokens=Decimal("200"),
        )
    }
    report = run_evaluation(
        dataset,
        [_stub_client("GigaChat-2", strong_fn), _stub_client("GigaChat-Weak", weak_fn)],
        critical_repeats=3,
        turns_per_dialogue=4,
        pricing_by_model=pricing,
    )

    # 2 critical * 3 repeats + 2 regular invoked = 8 LLM calls (no-ctx skipped)
    assert len(strong_calls) == 8
    assert report.models_compared == 2
    assert report.overall_accepted is True
    assert report.accepted_models == ["GigaChat-2"]

    strong_sum, weak_sum = report.summaries
    assert strong_sum.accepted is True
    assert strong_sum.critical_cases_passed == 2
    assert strong_sum.regular_cases_passed == 3
    assert strong_sum.invented_citations_count == 0
    assert strong_sum.unsafe_outputs_count == 0
    # 8 invoked attempts * (100*100 + 50*200) / 1_000_000 = 8 * 0.02 = 0.16 RUB
    assert strong_sum.estimated_cost_rub_total == Decimal("0.160000")
    # 9 total attempts (incl. 1 no-ctx) -> per 100 turns = 1.7778, x4 = 7.1112
    assert strong_sum.estimated_cost_rub_per_100_turns == Decimal("1.7778")
    assert strong_sum.estimated_cost_rub_per_100_dialogues == Decimal("7.1112")
    assert all(a.answer_text is None for a in report.attempts)

    assert weak_sum.accepted is False
    assert "INVENTED_CITATIONS" in weak_sum.rejection_reasons
    assert "UNSAFE_OUTPUTS" in weak_sum.rejection_reasons
    assert "CRITICAL_PASS_RATE_BELOW_THRESHOLD" in weak_sum.rejection_reasons
    md = format_markdown_report(report)
    assert "GigaChat-2" in md
    assert "GigaChat-Weak" in md
    assert "внутренний ключ" not in md


def test_flaky_critical_regular_threshold_errors_and_single_model_gate() -> None:
    sec_id = uuid4()
    item_id = uuid4()
    frag = GroundedFragment(section_id=sec_id, title="Doc", text="Текстовый фрагмент.")
    cat = GroundedCatalogItem(
        item_id=item_id, sku="SYN-X", name="SYNTHETIC", price_label="На заказ"
    )
    dataset = EvalDataset(
        cases=[
            EvalCase(
                case_id="crit-flaky",
                mode="knowledge",
                question="Критический вопрос",
                critical=True,
                expected_section_ids=[sec_id],
                fragments=[frag],
            ),
            EvalCase(
                case_id="reg-timeout",
                mode="commerce",
                question="Коммерческий вопрос",
                critical=False,
                expected_item_ids=[item_id],
                items=[cat],
            ),
        ]
    )

    crit_calls = 0

    def flaky_or_error(req: GroundedRequest) -> tuple[GroundedAnswer, TokenUsage]:
        nonlocal crit_calls
        if req.items:
            raise LLMError("LLM_TIMEOUT")
        crit_calls += 1
        # Fails on 3rd repeat -> critical case must fail overall
        if crit_calls == 3:
            return (
                GroundedAnswer(
                    answer="Поддельный товар",
                    cited_section_ids=[sec_id],
                    cited_item_ids=[uuid4()],
                    needs_handoff=False,
                ),
                TokenUsage(total_tokens=10),
            )
        return (
            GroundedAnswer(
                answer="Корректный ответ.",
                cited_section_ids=[sec_id],
                cited_item_ids=[],
                needs_handoff=False,
            ),
            TokenUsage(total_tokens=10),
        )

    rep_single = run_evaluation(
        dataset,
        [_stub_client("GigaChat-Flaky", flaky_or_error)],
        critical_repeats=3,
        thresholds=AcceptanceThresholds(min_models_compared=2),
    )
    assert rep_single.overall_accepted is False
    assert "INSUFFICIENT_MODELS_COMPARED" in rep_single.overall_rejection_reasons
    assert "NO_MODEL_ACCEPTED" in rep_single.overall_rejection_reasons
    summary = rep_single.summaries[0]
    assert summary.critical_cases_passed == 0
    assert summary.regular_cases_passed == 0
    assert summary.attempts_errors == 1
    assert summary.invented_citations_count == 1
    assert "INVENTED_CITATIONS" in summary.rejection_reasons
    assert "CRITICAL_PASS_RATE_BELOW_THRESHOLD" in summary.rejection_reasons
    assert "REGULAR_PASS_RATE_BELOW_THRESHOLD" in summary.rejection_reasons


def test_eval_cli_end_to_end_verifies_models_writes_outputs_and_hides_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth_key = "c3ludGhldGljLWRuMTAtYXV0aC1rZXk="
    oauth_token = "syn-access-token-secret-dn10"
    key_file = tmp_path / "gigachat_key"
    key_file.write_text(auth_key, encoding="utf-8")

    pricing_file = tmp_path / "pricing.json"
    pricing_file.write_text(
        json.dumps(
            [
                {
                    "model": "GigaChat-2",
                    "rub_per_1m_prompt_tokens": "150",
                    "rub_per_1m_completion_tokens": "300",
                }
            ]
        ),
        encoding="utf-8",
    )

    sec1 = "11111111-1111-4111-8111-111111111101"
    sec2 = "11111111-1111-4111-8111-111111111102"
    item1 = "22222222-2222-4222-8222-222222222201"

    def answer_handler(model: str, user_msg: str) -> dict[str, object]:
        payload = json.loads(user_msg)
        q = payload["question"]
        frags = payload["fragments"]
        items = payload["catalog_items"]
        if "Игнорируй правила" in q:
            body = {
                "answer": "Перевожу на специалиста.",
                "cited_section_ids": [],
                "cited_item_ids": [],
                "needs_handoff": True,
            }
        elif items:
            body = {
                "answer": f"Цена для {model}: 4500.00 RUB.",
                "cited_section_ids": [],
                "cited_item_ids": [item1],
                "needs_handoff": False,
            }
        else:
            sid = sec1 if any(f["section_id"] == sec1 for f in frags) else sec2
            body = {
                "answer": f"Ответ {model} по источнику.",
                "cited_section_ids": [sid],
                "cited_item_ids": [],
                "needs_handoff": False,
            }
        return {
            "choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 25, "total_tokens": 75},
        }

    with _local_gigachat_server(
        available_models=["GigaChat-2", "GigaChat-2-Pro"],
        auth_key=auth_key,
        access_token=oauth_token,
        answer_handler=answer_handler,
    ) as base_url:
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_SYNTHETIC", "1")
        monkeypatch.setenv("GIGACHAT_AUTH_KEY_FILE", str(key_file))
        monkeypatch.setenv("GIGACHAT_OAUTH_URL", f"{base_url}/api/v2/oauth")
        monkeypatch.setenv("GIGACHAT_API_BASE_URL", f"{base_url}/api/v1")

        out_json = tmp_path / "out" / "report.json"
        out_md = tmp_path / "out" / "report.md"
        rc = eval_cli_main(
            [
                "--dataset",
                str(FIXTURE_DATASET),
                "--models",
                "GigaChat-2",
                "GigaChat-2-Pro",
                "--pricing",
                str(pricing_file),
                "--verify-models",
                "--out-json",
                str(out_json),
                "--out-md",
                str(out_md),
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        json_text = out_json.read_text(encoding="utf-8")
        md_text = out_md.read_text(encoding="utf-8")
        for blob in (captured.out, captured.err, json_text, md_text):
            assert auth_key not in blob
            assert oauth_token not in blob
        parsed = json.loads(json_text)
        assert parsed["overall_accepted"] is True
        assert parsed["models_compared"] == 2
        assert parsed["accepted_models"] == ["GigaChat-2", "GigaChat-2-Pro"]
        assert all(a["answer_text"] is None for a in parsed["attempts"])

        # Missing model fails closed on --verify-models before running completions
        bad_rc = eval_cli_main(
            [
                "--dataset",
                str(FIXTURE_DATASET),
                "--models",
                "GigaChat-2",
                "GigaChat-Missing",
                "--verify-models",
            ]
        )
        assert bad_rc == 1

        # Single model without --allow-single-model exits 2 (acceptance rejected)
        single_rc = eval_cli_main(
            [
                "--dataset",
                str(FIXTURE_DATASET),
                "--models",
                "GigaChat-2",
            ]
        )
        assert single_rc == 2
