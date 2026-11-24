"""CLI entrypoint for DN-10 multi-model GigaChat acceptance evaluation."""

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from app.core.db import Database, DatabaseConfigurationError, DatabaseSettings
from app.modules.llm.budget import InMemoryBudgetLedger
from app.modules.llm.config import LLMConfigurationError, LLMSettings
from app.modules.llm.eval_runner import (
    AcceptanceThresholds,
    ContextResolver,
    EvalCase,
    EvalDataset,
    ModelPricing,
    format_markdown_report,
    resolve_context_from_db,
    run_evaluation,
)
from app.modules.llm.gigachat import GigaChatClient
from app.modules.llm.schemas import (
    GroundedCatalogItem,
    GroundedFragment,
    LLMError,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run DN-10 multi-model grounded GigaChat evaluation."
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--critical-repeats", type=int, default=3)
    parser.add_argument("--pricing", type=Path, default=None)
    parser.add_argument("--turns-per-dialogue", type=int, default=4)
    parser.add_argument("--verify-models", action="store_true")
    parser.add_argument("--allow-single-model", action="store_true")
    parser.add_argument("--use-db", action="store_true")
    parser.add_argument("--include-answers", action="store_true")
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--out-md", type=Path, default=None)
    return parser


def _load_dataset(path: Path) -> EvalDataset:
    raw = path.read_text(encoding="utf-8")
    return EvalDataset.model_validate_json(raw)


def _load_pricing(path: Path) -> dict[str, ModelPricing]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    adapter = TypeAdapter(list[ModelPricing])
    entries = adapter.validate_python(raw)
    return {entry.model: entry for entry in entries}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not 1 <= args.critical_repeats <= 10:
        print("ERROR: --critical-repeats must be between 1 and 10", file=sys.stderr)
        return 1
    if not 1 <= args.turns_per_dialogue <= 50:
        print("ERROR: --turns-per-dialogue must be between 1 and 50", file=sys.stderr)
        return 1

    model_names: list[str] = []
    seen_models: set[str] = set()
    for raw_name in args.models:
        cleaned = raw_name.strip()
        if not cleaned or len(cleaned) > 100:
            print("ERROR: invalid model name in --models", file=sys.stderr)
            return 1
        if cleaned not in seen_models:
            seen_models.add(cleaned)
            model_names.append(cleaned)

    try:
        dataset = _load_dataset(args.dataset)
    except (OSError, UnicodeError, ValidationError):
        print("ERROR: invalid or unreadable dataset file", file=sys.stderr)
        return 1

    pricing_by_model: dict[str, ModelPricing] = {}
    if args.pricing is not None:
        try:
            pricing_by_model = _load_pricing(args.pricing)
        except (OSError, UnicodeError, ValueError, ValidationError):
            print("ERROR: invalid or unreadable pricing file", file=sys.stderr)
            return 1

    try:
        settings = LLMSettings.from_environment()
    except LLMConfigurationError:
        print("ERROR: invalid LLM configuration", file=sys.stderr)
        return 1
    if settings is None:
        print("ERROR: LLM_ENABLED is off", file=sys.stderr)
        return 1

    clients: list[GigaChatClient] = []
    for model_name in model_names:
        try:
            model_settings = replace(settings, model=model_name)
        except LLMConfigurationError:
            print("ERROR: invalid model configuration", file=sys.stderr)
            return 1
        clients.append(
            GigaChatClient(
                model_settings,
                ledger=InMemoryBudgetLedger(
                    daily_token_budget=model_settings.daily_token_budget,
                    max_requests_per_minute=model_settings.max_requests_per_minute,
                ),
            )
        )

    if args.verify_models:
        try:
            available = set(clients[0].list_models())
        except LLMError as exc:
            print(f"ERROR: failed to list models ({exc.code})", file=sys.stderr)
            return 1
        missing = [m for m in model_names if m not in available]
        if missing:
            print(
                f"ERROR: requested models unavailable: {', '.join(missing)}",
                file=sys.stderr,
            )
            return 1

    db: Database | None = None
    context_resolver: ContextResolver | None = None
    if args.use_db:
        try:
            db = Database(DatabaseSettings.from_environment())
        except DatabaseConfigurationError:
            print("ERROR: invalid database configuration", file=sys.stderr)
            return 1
        active_db = db

        def _resolve(
            case: EvalCase,
        ) -> tuple[list[GroundedFragment], list[GroundedCatalogItem]]:
            with active_db.unit_of_work() as session:
                return resolve_context_from_db(session, case)

        context_resolver = _resolve

    try:
        thresholds = AcceptanceThresholds(
            min_models_compared=1 if args.allow_single_model else 2
        )
        report = run_evaluation(
            dataset,
            clients,
            context_resolver=context_resolver,
            critical_repeats=args.critical_repeats,
            turns_per_dialogue=args.turns_per_dialogue,
            pricing_by_model=pricing_by_model,
            thresholds=thresholds,
            include_answers=args.include_answers,
        )
    finally:
        if db is not None:
            db.engine.dispose()

    markdown = format_markdown_report(report)
    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(
            report.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    if args.out_md is not None:
        args.out_md.parent.mkdir(parents=True, exist_ok=True)
        args.out_md.write_text(markdown + "\n", encoding="utf-8")

    print(markdown)
    return 0 if report.overall_accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
