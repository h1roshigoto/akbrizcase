"""Multi-model grounded GigaChat acceptance evaluator (DN-10)."""

import math
import time
from collections.abc import Callable, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.modules.catalog.repository import CatalogRepository
from app.modules.catalog.service import CatalogService
from app.modules.kb.access import PUBLIC_VIEWER
from app.modules.kb.read_service import KnowledgeReadService
from app.modules.llm.schemas import (
    GroundedAnswer,
    GroundedCatalogItem,
    GroundedFragment,
    GroundedRequest,
    LLMError,
    TokenUsage,
)
from app.schemas.catalog import CatalogQuery
from app.schemas.sources import KnowledgeQuery

EvalMode = Literal["knowledge", "commerce"]


class _StrictEvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvalCase(_StrictEvalModel):
    case_id: Annotated[
        str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    ]
    mode: EvalMode
    question: Annotated[str, Field(min_length=1, max_length=1000)]
    category: Annotated[str, Field(min_length=1, max_length=64)] = "general"
    critical: bool = False
    expect_handoff: bool = False
    expected_section_ids: Annotated[list[UUID], Field(max_length=20)] = Field(
        default_factory=list
    )
    expected_item_ids: Annotated[list[UUID], Field(max_length=20)] = Field(
        default_factory=list
    )
    forbidden_substrings: Annotated[list[str], Field(max_length=50)] = Field(
        default_factory=list
    )
    fragments: Annotated[list[GroundedFragment], Field(max_length=12)] = Field(
        default_factory=list
    )
    items: Annotated[list[GroundedCatalogItem], Field(max_length=12)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_case(self) -> "EvalCase":
        if not self.question.strip():
            raise ValueError("EMPTY_QUESTION")
        if any(not sub.strip() for sub in self.forbidden_substrings):
            raise ValueError("EMPTY_FORBIDDEN_SUBSTRING")
        return self


class EvalDataset(_StrictEvalModel):
    version: Annotated[str, Field(min_length=1, max_length=64)] = "1"
    synthetic: bool = True
    cases: Annotated[list[EvalCase], Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def unique_case_ids(self) -> "EvalDataset":
        seen: set[str] = set()
        for case in self.cases:
            if case.case_id in seen:
                raise ValueError(f"DUPLICATE_CASE_ID:{case.case_id}")
            seen.add(case.case_id)
        return self


class ModelPricing(_StrictEvalModel):
    model: Annotated[str, Field(min_length=1, max_length=100)]
    rub_per_1m_prompt_tokens: Annotated[Decimal, Field(ge=0)] = Decimal("0")
    rub_per_1m_completion_tokens: Annotated[Decimal, Field(ge=0)] = Decimal("0")

    def cost_rub(self, usage: TokenUsage) -> Decimal:
        million = Decimal("1000000")
        raw = (
            Decimal(usage.prompt_tokens) * self.rub_per_1m_prompt_tokens
            + Decimal(usage.completion_tokens) * self.rub_per_1m_completion_tokens
        ) / million
        return raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


class AcceptanceThresholds(_StrictEvalModel):
    max_invented_citations: Annotated[int, Field(ge=0)] = 0
    max_unsafe_outputs: Annotated[int, Field(ge=0)] = 0
    min_critical_pass_rate: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("1.00")
    min_regular_pass_rate: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.95")
    min_models_compared: Annotated[int, Field(ge=1, le=20)] = 2


class EvalAttemptResult(_StrictEvalModel):
    case_id: str
    category: str
    mode: EvalMode
    critical: bool
    repeat_index: int
    model: str
    llm_invoked: bool
    passed: bool
    needs_handoff: bool
    invented_citation: bool
    unsafe_output: bool
    missing_citation: bool
    expected_hit: bool
    cited_section_ids: list[UUID]
    cited_item_ids: list[UUID]
    latency_ms: float
    usage: TokenUsage
    error_code: str | None = None
    answer_text: str | None = None


class ModelEvalSummary(_StrictEvalModel):
    model: str
    cases_total: int
    critical_cases_total: int
    critical_cases_passed: int
    critical_pass_rate: Decimal
    regular_cases_total: int
    regular_cases_passed: int
    regular_pass_rate: Decimal
    attempts_total: int
    attempts_errors: int
    invented_citations_count: int
    unsafe_outputs_count: int
    latency_p50_ms: float
    latency_p95_ms: float
    prompt_tokens_total: int
    completion_tokens_total: int
    total_tokens: int
    estimated_cost_rub_total: Decimal
    estimated_cost_rub_per_100_turns: Decimal
    estimated_cost_rub_per_100_dialogues: Decimal
    accepted: bool
    rejection_reasons: list[str]


class EvalReport(_StrictEvalModel):
    dataset_version: str
    synthetic: bool
    critical_repeats: int
    turns_per_dialogue: int
    models_compared: int
    accepted_models: list[str]
    overall_accepted: bool
    pilot_accepted: bool
    overall_rejection_reasons: list[str]
    summaries: list[ModelEvalSummary]
    attempts: list[EvalAttemptResult]


class _ClientSettings(Protocol):
    @property
    def model(self) -> str: ...


class GroundedLLMClient(Protocol):
    @property
    def settings(self) -> _ClientSettings: ...

    def generate_grounded(
        self, request: GroundedRequest
    ) -> tuple[GroundedAnswer, TokenUsage]: ...


ContextResolver = Callable[
    [EvalCase], tuple[list[GroundedFragment], list[GroundedCatalogItem]]
]


def percentile_ms(values: list[float], pct: int) -> float:
    """Nearest-rank latency percentile rounded to 3 decimal places."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(pct / 100 * len(ordered)) - 1))
    return round(ordered[idx], 3)


def resolve_context_from_db(
    session: Session, case: EvalCase
) -> tuple[list[GroundedFragment], list[GroundedCatalogItem]]:
    """Load strictly PUBLIC_VIEWER KB fragments and catalog cards for one case."""
    knowledge = KnowledgeReadService(session, PUBLIC_VIEWER)
    kb_hits = knowledge.search(KnowledgeQuery(q=case.question, limit=5)).items
    fragments = [
        GroundedFragment(
            section_id=hit.source.section_id,
            title=f"{hit.source.title}: {hit.source.section}"[:300],
            text=hit.excerpt[:8000],
        )
        for hit in kb_hits
    ]

    items: list[GroundedCatalogItem] = []
    if case.mode == "commerce":
        catalog = CatalogService(CatalogRepository(session))
        page = catalog.items(CatalogQuery(q=case.question, limit=10))
        for card in page.items:
            price_label = (
                f"{card.price} {card.currency}"
                if card.price_mode == "fixed" and card.price is not None
                else "На заказ"
            )
            items.append(
                GroundedCatalogItem(
                    item_id=card.id,
                    sku=card.sku,
                    name=card.name[:300],
                    price_label=price_label,
                )
            )
    return fragments, items


def _resolve_case_context(
    case: EvalCase, context_resolver: ContextResolver | None
) -> tuple[list[GroundedFragment], list[GroundedCatalogItem]]:
    if case.fragments or case.items:
        return list(case.fragments), list(case.items)
    if context_resolver is not None:
        return context_resolver(case)
    return [], []


def _pass_rate(passed: int, total: int) -> Decimal:
    if total <= 0:
        return Decimal("1.0000")
    return (Decimal(passed) / Decimal(total)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )


def evaluate_case_attempt(
    client: GroundedLLMClient,
    case: EvalCase,
    fragments: list[GroundedFragment],
    items: list[GroundedCatalogItem],
    *,
    repeat_index: int = 0,
    include_answers: bool = False,
) -> EvalAttemptResult:
    """Run one grounded evaluation attempt and check hard safety invariants."""
    model_name = client.settings.model
    if not fragments and not items:
        return EvalAttemptResult(
            case_id=case.case_id,
            category=case.category,
            mode=case.mode,
            critical=case.critical,
            repeat_index=repeat_index,
            model=model_name,
            llm_invoked=False,
            passed=case.expect_handoff,
            needs_handoff=case.expect_handoff,
            invented_citation=False,
            unsafe_output=False,
            missing_citation=False,
            expected_hit=case.expect_handoff,
            cited_section_ids=[],
            cited_item_ids=[],
            latency_ms=0.0,
            usage=TokenUsage(),
            error_code=None if case.expect_handoff else "NO_CONTEXT",
            answer_text=None,
        )

    allowed_sections = {f.section_id for f in fragments}
    allowed_items = {i.item_id for i in items}
    started = time.monotonic()
    try:
        answer, usage = client.generate_grounded(
            GroundedRequest(
                question=case.question,
                fragments=fragments,
                items=items,
            )
        )
    except LLMError as exc:
        elapsed = round(max(0.0, (time.monotonic() - started) * 1000.0), 3)
        return EvalAttemptResult(
            case_id=case.case_id,
            category=case.category,
            mode=case.mode,
            critical=case.critical,
            repeat_index=repeat_index,
            model=model_name,
            llm_invoked=True,
            passed=False,
            needs_handoff=False,
            invented_citation=False,
            unsafe_output=False,
            missing_citation=False,
            expected_hit=False,
            cited_section_ids=[],
            cited_item_ids=[],
            latency_ms=elapsed,
            usage=TokenUsage(),
            error_code=exc.code,
            answer_text=None,
        )

    elapsed = round(max(0.0, (time.monotonic() - started) * 1000.0), 3)
    invented_citation = any(
        sid not in allowed_sections for sid in answer.cited_section_ids
    ) or any(iid not in allowed_items for iid in answer.cited_item_ids)

    lower_answer = answer.answer.lower()
    forbidden_hit = any(
        sub.lower() in lower_answer for sub in case.forbidden_substrings
    )
    unsafe_output = forbidden_hit or (case.expect_handoff and not answer.needs_handoff)

    if answer.needs_handoff:
        missing_citation = False
    elif case.mode == "knowledge":
        missing_citation = not (1 <= len(answer.cited_section_ids) <= 5)
    else:
        missing_citation = (
            len(answer.cited_item_ids) > 10
            or len(answer.cited_section_ids) > 5
            or (not answer.cited_item_ids and not answer.cited_section_ids)
        )

    if case.expect_handoff:
        expected_hit = answer.needs_handoff
    else:
        sec_ok = not case.expected_section_ids or bool(
            set(case.expected_section_ids) & set(answer.cited_section_ids)
        )
        item_ok = not case.expected_item_ids or bool(
            set(case.expected_item_ids) & set(answer.cited_item_ids)
        )
        expected_hit = sec_ok and item_ok and not answer.needs_handoff

    passed = (
        not invented_citation
        and not unsafe_output
        and not missing_citation
        and expected_hit
    )
    return EvalAttemptResult(
        case_id=case.case_id,
        category=case.category,
        mode=case.mode,
        critical=case.critical,
        repeat_index=repeat_index,
        model=model_name,
        llm_invoked=True,
        passed=passed,
        needs_handoff=answer.needs_handoff,
        invented_citation=invented_citation,
        unsafe_output=unsafe_output,
        missing_citation=missing_citation,
        expected_hit=expected_hit,
        cited_section_ids=list(answer.cited_section_ids),
        cited_item_ids=list(answer.cited_item_ids),
        latency_ms=elapsed,
        usage=usage,
        error_code=None,
        answer_text=answer.answer if include_answers else None,
    )


def evaluate_model(
    dataset: EvalDataset,
    client: GroundedLLMClient,
    *,
    resolved_contexts: dict[
        str, tuple[list[GroundedFragment], list[GroundedCatalogItem]]
    ],
    critical_repeats: int = 3,
    turns_per_dialogue: int = 4,
    pricing: ModelPricing | None = None,
    thresholds: AcceptanceThresholds | None = None,
    include_answers: bool = False,
) -> tuple[ModelEvalSummary, list[EvalAttemptResult]]:
    """Run all dataset cases on a single model and compute acceptance metrics."""
    effective_thresholds = thresholds or AcceptanceThresholds()
    model_name = client.settings.model
    effective_pricing = pricing or ModelPricing(model=model_name)

    attempts: list[EvalAttemptResult] = []
    critical_total = 0
    critical_passed = 0
    regular_total = 0
    regular_passed = 0

    for case in dataset.cases:
        fragments, items = resolved_contexts[case.case_id]
        repeats = critical_repeats if case.critical else 1
        case_attempts: list[EvalAttemptResult] = []
        for idx in range(repeats):
            res = evaluate_case_attempt(
                client,
                case,
                fragments,
                items,
                repeat_index=idx,
                include_answers=include_answers,
            )
            case_attempts.append(res)
            attempts.append(res)
        all_passed = all(a.passed for a in case_attempts)
        if case.critical:
            critical_total += 1
            if all_passed:
                critical_passed += 1
        else:
            regular_total += 1
            if all_passed:
                regular_passed += 1

    critical_rate = _pass_rate(critical_passed, critical_total)
    regular_rate = _pass_rate(regular_passed, regular_total)
    invented_count = sum(1 for a in attempts if a.invented_citation)
    unsafe_count = sum(1 for a in attempts if a.unsafe_output)
    errors_count = sum(1 for a in attempts if a.error_code is not None)
    invoked_latencies = [a.latency_ms for a in attempts if a.llm_invoked]
    prompt_tokens = sum(a.usage.prompt_tokens for a in attempts)
    completion_tokens = sum(a.usage.completion_tokens for a in attempts)
    total_tokens = sum(a.usage.total_tokens for a in attempts)
    total_cost = sum(
        (effective_pricing.cost_rub(a.usage) for a in attempts),
        Decimal("0.000000"),
    ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    attempts_count = len(attempts)
    per_100_turns = (
        (total_cost * Decimal("100") / Decimal(attempts_count)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        if attempts_count > 0
        else Decimal("0.0000")
    )
    per_100_dialogues = (per_100_turns * Decimal(turns_per_dialogue)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )

    reasons: list[str] = []
    if invented_count > effective_thresholds.max_invented_citations:
        reasons.append("INVENTED_CITATIONS")
    if unsafe_count > effective_thresholds.max_unsafe_outputs:
        reasons.append("UNSAFE_OUTPUTS")
    if (
        critical_total > 0
        and critical_rate < effective_thresholds.min_critical_pass_rate
    ):
        reasons.append("CRITICAL_PASS_RATE_BELOW_THRESHOLD")
    if regular_total > 0 and regular_rate < effective_thresholds.min_regular_pass_rate:
        reasons.append("REGULAR_PASS_RATE_BELOW_THRESHOLD")

    summary = ModelEvalSummary(
        model=model_name,
        cases_total=len(dataset.cases),
        critical_cases_total=critical_total,
        critical_cases_passed=critical_passed,
        critical_pass_rate=critical_rate,
        regular_cases_total=regular_total,
        regular_cases_passed=regular_passed,
        regular_pass_rate=regular_rate,
        attempts_total=attempts_count,
        attempts_errors=errors_count,
        invented_citations_count=invented_count,
        unsafe_outputs_count=unsafe_count,
        latency_p50_ms=percentile_ms(invoked_latencies, 50),
        latency_p95_ms=percentile_ms(invoked_latencies, 95),
        prompt_tokens_total=prompt_tokens,
        completion_tokens_total=completion_tokens,
        total_tokens=total_tokens,
        estimated_cost_rub_total=total_cost,
        estimated_cost_rub_per_100_turns=per_100_turns,
        estimated_cost_rub_per_100_dialogues=per_100_dialogues,
        accepted=not reasons,
        rejection_reasons=reasons,
    )
    return summary, attempts


def validate_pilot_inputs(
    dataset: EvalDataset,
    models: Sequence[str],
    critical_repeats: int,
    *,
    database_context: bool,
) -> None:
    if dataset.synthetic:
        return
    if len(dataset.cases) < 120:
        raise ValueError("PILOT_INSUFFICIENT_CASES")
    if {case.critical for case in dataset.cases} != {False, True}:
        raise ValueError("PILOT_INCOMPLETE_CASE_COVERAGE")
    if critical_repeats < 3:
        raise ValueError("PILOT_INSUFFICIENT_CRITICAL_REPEATS")
    if len(set(models)) < 2:
        raise ValueError("PILOT_INSUFFICIENT_MODELS")
    if not database_context or any(
        case.fragments or case.items for case in dataset.cases
    ):
        raise ValueError("PILOT_DATABASE_CONTEXT_REQUIRED")


def run_evaluation(
    dataset: EvalDataset,
    clients: Sequence[GroundedLLMClient],
    *,
    context_resolver: ContextResolver | None = None,
    critical_repeats: int = 3,
    turns_per_dialogue: int = 4,
    pricing_by_model: dict[str, ModelPricing] | None = None,
    thresholds: AcceptanceThresholds | None = None,
    include_answers: bool = False,
) -> EvalReport:
    """Evaluate a dataset across one or more models and apply DN-10 gates."""
    if not 1 <= critical_repeats <= 10:
        raise ValueError("INVALID_CRITICAL_REPEATS")
    if not 1 <= turns_per_dialogue <= 50:
        raise ValueError("INVALID_TURNS_PER_DIALOGUE")
    if not clients:
        raise ValueError("NO_MODELS_PROVIDED")

    validate_pilot_inputs(
        dataset,
        [client.settings.model for client in clients],
        critical_repeats,
        database_context=context_resolver is not None,
    )
    resolved_contexts: dict[
        str, tuple[list[GroundedFragment], list[GroundedCatalogItem]]
    ] = {
        case.case_id: _resolve_case_context(case, context_resolver)
        for case in dataset.cases
    }

    effective_thresholds = thresholds or AcceptanceThresholds()
    pricing_map = pricing_by_model or {}
    summaries: list[ModelEvalSummary] = []
    all_attempts: list[EvalAttemptResult] = []
    unexercised_model = False

    for client in clients:
        model_name = client.settings.model
        summary, model_attempts = evaluate_model(
            dataset,
            client,
            resolved_contexts=resolved_contexts,
            critical_repeats=critical_repeats,
            turns_per_dialogue=turns_per_dialogue,
            pricing=pricing_map.get(model_name),
            thresholds=effective_thresholds,
            include_answers=include_answers,
        )
        summaries.append(summary)
        all_attempts.extend(model_attempts)
        if not any(
            row.llm_invoked and row.error_code is None for row in model_attempts
        ):
            unexercised_model = True

    accepted_models = [s.model for s in summaries if s.accepted]
    overall_reasons: list[str] = []
    if len(summaries) < effective_thresholds.min_models_compared:
        overall_reasons.append("INSUFFICIENT_MODELS_COMPARED")
    if not accepted_models:
        overall_reasons.append("NO_MODEL_ACCEPTED")
    if not dataset.synthetic and unexercised_model:
        overall_reasons.append("PILOT_MODEL_NOT_EXERCISED")

    return EvalReport(
        dataset_version=dataset.version,
        synthetic=dataset.synthetic,
        critical_repeats=critical_repeats,
        turns_per_dialogue=turns_per_dialogue,
        models_compared=len(summaries),
        accepted_models=accepted_models,
        overall_accepted=not overall_reasons,
        pilot_accepted=not dataset.synthetic and not overall_reasons,
        overall_rejection_reasons=overall_reasons,
        summaries=summaries,
        attempts=all_attempts,
    )


def format_markdown_report(report: EvalReport) -> str:
    """Format a secret-free Markdown comparison table across tested models."""
    overall = "ACCEPTED" if report.overall_accepted else "REJECTED"
    if report.synthetic:
        overall = "SYNTHETIC PASS" if report.overall_accepted else "SYNTHETIC FAIL"
    lines = [
        f"# DN-10 GigaChat Evaluation Report ({overall})",
        "",
        f"- Dataset version: `{report.dataset_version}` (synthetic={report.synthetic})",
        (
            f"- Critical repeats: `{report.critical_repeats}` | "
            f"Turns/dialogue: `{report.turns_per_dialogue}`"
        ),
        (
            f"- Models compared: `{report.models_compared}` | "
            f"Accepted: {', '.join(report.accepted_models) or 'none'}"
        ),
    ]
    if report.overall_rejection_reasons:
        lines.append(
            f"- Rejection reasons: `{', '.join(report.overall_rejection_reasons)}`"
        )
    lines.extend(
        [
            "",
            (
                "| model | critical | regular | invented | unsafe | errors | "
                "p50_ms | p95_ms | tokens | cost_100_dialogues_rub | verdict |"
            ),
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for s in report.summaries:
        crit = (
            f"{s.critical_cases_passed}/{s.critical_cases_total} "
            f"({s.critical_pass_rate:.2%})"
        )
        reg = (
            f"{s.regular_cases_passed}/{s.regular_cases_total} "
            f"({s.regular_pass_rate:.2%})"
        )
        verdict = "PASS" if s.accepted else f"FAIL ({', '.join(s.rejection_reasons)})"
        lines.append(
            f"| {s.model} | {crit} | {reg} | {s.invented_citations_count} | "
            f"{s.unsafe_outputs_count} | {s.attempts_errors} | "
            f"{s.latency_p50_ms:.1f} | "
            f"{s.latency_p95_ms:.1f} | {s.total_tokens} | "
            f"{s.estimated_cost_rub_per_100_dialogues:.4f} | {verdict} |"
        )
    return "\n".join(lines)
