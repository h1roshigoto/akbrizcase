.PHONY: sync run test lint types
PYTEST_ARGS ?=

sync:
	uv sync --locked

run:
	uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers

test:
	uv run --locked pytest $(PYTEST_ARGS)

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .

types:
	uv run --locked mypy

.PHONY: migrate
migrate:
	uv run --locked python -m app.core.db_migrate

.PHONY: seed-demo seed-test
seed-demo:
	uv run --locked python -m app.modules.catalog.seed --mode demo

seed-test:
	uv run --locked python -m app.modules.catalog.seed --mode test
