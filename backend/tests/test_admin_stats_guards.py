"""Fast boundary/guard checks; SQL correctness is accepted separately on PG16."""

import ast
import asyncio
from datetime import UTC, date, datetime, timedelta
from inspect import unwrap
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from app.admin.stats import StatsView
from app.schemas.admin_stats import OrderStats, StatsPeriod

INVALID_QUERIES = [
    [("from", "2026-01-01")],
    [("to", "2026-01-01")],
    [("from", "2026-01-01"), ("from", "2026-01-02")],
    [("from", "2026-01-01"), ("to", "2026-01-02"), ("to", "2026-01-02")],
    [("from", "2026-01-01"), ("to", "2026-01-02"), ("group", "email")],
    [("from", "2026-1-01"), ("to", "2026-01-02")],
    [("from", "2026-01-01T00:00:00Z"), ("to", "2026-01-02")],
    [("from", "2026-02-29"), ("to", "2026-03-01")],
    [("from", "2026-01-02"), ("to", "2026-01-01")],
    [("from", "2026-01-01"), ("to", "2026-04-01")],
    [("from", ""), ("to", "2026-01-01")],
    [("from", "9999-12-31"), ("to", "9999-12-31")],
    [("from", "２０２６-01-01"), ("to", "2026-01-01")],
    [("email", "synthetic-stats@example.invalid")],
]


@pytest.mark.parametrize("pairs", INVALID_QUERIES)
def test_rejects_ambiguous_unbounded_and_noncanonical_dates(
    pairs: list[tuple[str, str]],
) -> None:
    with pytest.raises(ValueError):
        StatsPeriod.from_query(pairs)


def test_default_utc_window_and_inclusive_90_day_leap_boundary() -> None:
    before = datetime.now(UTC).date()
    default = StatsPeriod.from_query([])
    assert before <= default.end <= datetime.now(UTC).date()
    assert (default.end - default.start).days == 29
    period = StatsPeriod.from_query([("to", "2024-03-30"), ("from", "2024-01-01")])
    assert (period.stop_utc - period.start_utc) == timedelta(days=90)
    assert period.start_utc == datetime(2024, 1, 1, tzinfo=UTC)
    assert period.stop_utc == datetime(2024, 3, 31, tzinfo=UTC)
    assert StatsPeriod.from_query(
        [("from", "2024-02-29"), ("to", "2024-02-29")]
    ).start == date(2024, 2, 29)
    with pytest.raises(ValidationError):
        StatsPeriod(start=date(2024, 1, 1), end=date(2024, 3, 31))


def test_closed_count_projection_rejects_personal_fields() -> None:
    counts = dict.fromkeys(OrderStats.model_fields, 0)
    assert OrderStats.model_validate(counts).total == 0
    with pytest.raises(ValidationError):
        OrderStats.model_validate(counts | {"email": "synthetic@example.invalid"})
    with pytest.raises(ValidationError):
        OrderStats.model_validate(counts | {"total": -1})


def call_stats_handler(request: Request) -> Response:
    # Unit-test the handler body only. SQLAdmin's @expose uses functools.wraps;
    # inspect.unwrap is independent of whether another test registered the view.
    # Real route authentication remains covered by test_admin_stats.py on PG16.
    handler = unwrap(StatsView.stats_page)
    assert handler is not StatsView.stats_page
    response: Response = asyncio.run(handler(StatsView(), request))
    return response


def test_view_validation_never_queries_and_errors_never_echo_input(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    read = Mock(side_effect=AssertionError("Invalid range must not query"))
    monkeypatch.setattr("app.admin.stats.read_statistics", read)
    request = Request(
        {
            "type": "http",
            "query_string": b"email=synthetic-stats%40example.invalid",
            "headers": [],
        }
    )
    request.state.request_id = uuid4()
    response = call_stats_handler(request)
    assert response.status_code == 422
    assert b"synthetic-stats" not in response.body
    assert "synthetic-stats" not in caplog.text
    read.assert_not_called()


def test_view_database_failure_is_not_success_or_raw_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    read = Mock(
        side_effect=SQLAlchemyError(
            "synthetic-db-secret@example.invalid SELECT private"
        )
    )
    monkeypatch.setattr("app.admin.stats.read_statistics", read)
    request = Request({"type": "http", "query_string": b"", "headers": []})
    request.state.request_id = uuid4()
    request.state.staff_db = Mock(spec=Session)
    response = call_stats_handler(request)
    assert response.status_code == 503
    assert b"synthetic-db-secret" not in response.body
    assert b"SELECT" not in response.body
    assert "synthetic-db-secret" not in caplog.text
    assert "SELECT private" not in caplog.text
    read.assert_called_once()


def test_existing_ci_collects_stats_in_core_without_another_job() -> None:
    root = Path(__file__).resolve().parents[2]
    # Parse the literal suite setup without importing/executing the Docker runner.
    tree = ast.parse((root / "scripts/db_integration.py").read_text())
    suites = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "SUITES"
            for target in node.targets
        )
    )
    path = "tests/integration/test_admin_stats.py"
    assert all(path not in paths for paths in suites.values())
    core = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id == "SUITES"
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "core"
            for target in node.targets
        )
    )
    # Evaluate only the suite assignment, so a changed exclusion really fails.
    namespace = {"SUITES": suites}
    exec(
        compile(ast.Module(body=[core], type_ignores=[]), "<suite-selection>", "exec"),
        namespace,
    )
    assert not any(
        path.startswith(exclusion.removeprefix("--ignore="))
        for exclusion in suites["core"]
    )
    workflow = (root / ".github/workflows/ci.yml").read_text()
    assert "run: make integration-ci" in workflow
    assert (root / "backend" / path).is_file()
