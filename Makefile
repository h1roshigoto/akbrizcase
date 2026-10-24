COMPOSE ?= docker compose

.PHONY: init-secrets check-secrets config build up stop down restart ps logs smoke

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
