"""Strict DTOs, closed error codes, and JSON output parsing for GigaChat LLM."""

import json
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MAX_RAW_OUTPUT_CHARS = 32768
MAX_ANSWER_CHARS = 4000

LLM_ERROR_CODES = frozenset(
    {
        "LLM_DISABLED",
        "LLM_CONFIG_INVALID",
        "LLM_AUTH_FAILED",
        "LLM_RATE_LIMITED",
        "LLM_BUDGET_EXCEEDED",
        "LLM_TIMEOUT",
        "LLM_UPSTREAM_ERROR",
        "LLM_UNAVAILABLE",
        "LLM_MODEL_UNAVAILABLE",
        "LLM_MALFORMED_OUTPUT",
        "LLM_STORAGE_ERROR",
    }
)


class LLMError(Exception):
    """Fail-closed LLM failure carrying only a closed code (never secrets/PII)."""

    def __init__(self, code: str) -> None:
        safe = code if code in LLM_ERROR_CODES else "LLM_UPSTREAM_ERROR"
        self.code = safe
        super().__init__(safe)


class StrictLLMModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GroundedFragment(StrictLLMModel):
    section_id: UUID
    title: Annotated[str, Field(min_length=1, max_length=300)]
    text: Annotated[str, Field(min_length=1, max_length=8000)]


class GroundedCatalogItem(StrictLLMModel):
    item_id: UUID
    sku: Annotated[str, Field(min_length=1, max_length=200)]
    name: Annotated[str, Field(min_length=1, max_length=300)]
    price_label: Annotated[str, Field(min_length=1, max_length=120)]


class GroundedRequest(StrictLLMModel):
    question: Annotated[str, Field(min_length=1, max_length=1000)]
    fragments: Annotated[list[GroundedFragment], Field(max_length=12)] = Field(
        default_factory=list
    )
    items: Annotated[list[GroundedCatalogItem], Field(max_length=12)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def require_meaningful_question(self) -> "GroundedRequest":
        if not self.question.strip():
            raise ValueError("EMPTY_QUESTION")
        return self


class GroundedAnswer(StrictLLMModel):
    answer: Annotated[str, Field(min_length=1, max_length=MAX_ANSWER_CHARS)]
    cited_section_ids: Annotated[list[UUID], Field(max_length=20)] = Field(
        default_factory=list
    )
    cited_item_ids: Annotated[list[UUID], Field(max_length=20)] = Field(
        default_factory=list
    )
    needs_handoff: bool = False

    @model_validator(mode="after")
    def clean_answer_and_dedupe(self) -> "GroundedAnswer":
        if not self.answer.strip():
            raise ValueError("EMPTY_ANSWER")
        object.__setattr__(
            self, "cited_section_ids", list(dict.fromkeys(self.cited_section_ids))
        )
        object.__setattr__(
            self, "cited_item_ids", list(dict.fromkeys(self.cited_item_ids))
        )
        return self


class TokenUsage(StrictLLMModel):
    prompt_tokens: Annotated[int, Field(ge=0)] = 0
    completion_tokens: Annotated[int, Field(ge=0)] = 0
    total_tokens: Annotated[int, Field(ge=0)] = 0


class LLMCallMetrics(StrictLLMModel):
    model: Annotated[str, Field(min_length=1, max_length=100)]
    outcome_code: Annotated[str, Field(min_length=1, max_length=64)]
    duration_ms: Annotated[float, Field(ge=0.0)]
    usage: TokenUsage = Field(default_factory=TokenUsage)


def _strip_json_fence(raw_text: str) -> str:
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if (
            len(lines) >= 2
            and lines[0].startswith("```")
            and lines[-1].strip() == "```"
        ):
            text = "\n".join(lines[1:-1]).strip()
    return text


def parse_grounded_output(raw_text: str) -> GroundedAnswer:
    """Parse strict structured JSON from model content without leaking content."""
    if not isinstance(raw_text, str) or len(raw_text) > MAX_RAW_OUTPUT_CHARS:
        raise LLMError("LLM_MALFORMED_OUTPUT")
    cleaned = _strip_json_fence(raw_text)
    if not cleaned:
        raise LLMError("LLM_MALFORMED_OUTPUT")
    try:
        payload = json.loads(cleaned)
        return GroundedAnswer.model_validate(payload)
    except (ValueError, ValidationError):
        raise LLMError("LLM_MALFORMED_OUTPUT") from None
