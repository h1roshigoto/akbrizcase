COMPOSE ?= docker compose
.DEFAULT_GOAL := init-secrets

.PHONY: init-secrets check-secrets config build up stop down restart ps logs smoke
.PHONY: test lint types backend-sync backend-test backend-lint backend-types
.PHONY: frontend-sync frontend-typecheck frontend-build

# Shared entry points for local development and GitHub Actions.
test: backend-test frontend-build

lint: backend-lint frontend-typecheck

types: backend-types frontend-typecheck

backend-sync:
	$(MAKE) -C backend sync

backend-test:
	$(MAKE) -C backend test

backend-lint:
	$(MAKE) -C backend lint

backend-types:
	$(MAKE) -C backend types

frontend-sync:
	npm --prefix frontend ci

frontend-typecheck:
	npm --prefix frontend run typecheck

frontend-build:
	npm --prefix frontend run build

init-secrets:
	python3 scripts/runtime_secret.py init

check-secrets:
	python3 scripts/runtime_secret.py check

config: check-secrets
	$(COMPOSE) config --quiet

build: check-secrets
	$(COMPOSE) build

up: check-secrets
	$(COMPOSE) up --build --detach --wait --wait-timeout 120

stop:
	$(COMPOSE) stop

down:
	$(COMPOSE) down

restart: check-secrets
	$(COMPOSE) restart

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs --tail=100

smoke: check-secrets
	python3 scripts/compose_smoke.py
