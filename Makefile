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

.PHONY: provision-db migrate integration integration-reset

# Explicit operator commands. Ordinary up/restart never run migrations.
provision-db: check-secrets
	$(COMPOSE) exec -T postgres sh /docker-entrypoint-initdb.d/010-provision.sh

migrate: check-secrets
	$(COMPOSE) run --rm migrate

integration:
	python3 scripts/db_integration.py $(INTEGRATION_ARGS)

.PHONY: integration-ci compose-smokes
# CI includes real file acceptance in the same two-stack concurrency budget.
integration-ci: check-secrets
	python3 scripts/db_integration.py --with-files

compose-smokes: check-secrets
	python3 scripts/ci_suites.py compose

integration-reset:
	python3 scripts/db_integration.py reset

.PHONY: seed-demo
# Requires an owner-applied database marker as documented in docs/MVP-006.md.
seed-demo: check-secrets
	APP_ENV=demo $(COMPOSE) run --rm seed --mode demo

.PHONY: local-tls https-up https-smoke
local-tls:
	python3 scripts/local_tls.py

https-up: check-secrets
	$(COMPOSE) -f docker-compose.yml -f compose.https.yml up --build --detach --wait --wait-timeout 120

https-smoke:
	python3 scripts/https_smoke.py

.PHONY: catalog-browser-smoke
catalog-browser-smoke:
	python3 scripts/catalog_browser_smoke.py

.PHONY: request-browser-smoke
request-browser-smoke:
	python3 scripts/request_browser_smoke.py

.PHONY: acceptance-guards vertical-smoke
acceptance-guards:
	python3 -m unittest discover -s scripts -p 'test_*.py'

# Extend MVP-013's real browser path, keeping a single source of E2E behavior.
vertical-smoke: acceptance-guards request-browser-smoke

.PHONY: status-browser-smoke
# Run every MVP-014 assertion first, then process that same synthetic order.
status-browser-smoke: acceptance-guards
	python3 scripts/request_browser_smoke.py --status

.PHONY: catalog-admin-browser-smoke
catalog-admin-browser-smoke: acceptance-guards
	python3 scripts/catalog_admin_browser_smoke.py

.PHONY: worker-smoke
worker-smoke: acceptance-guards
	python3 scripts/worker_smoke.py

.PHONY: notification-retry-browser-smoke
notification-retry-browser-smoke: acceptance-guards
	python3 scripts/notification_retry_browser_smoke.py

.PHONY: files-smoke
files-smoke: acceptance-guards
	python3 scripts/files_smoke.py
