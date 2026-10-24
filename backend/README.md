# Backend — MVP-001 и MVP-003

FastAPI-каркас с `GET /healthz`, `GET /readyz`, PostgreSQL 16 и общим UoW,
без бизнес-модулей.

Требуются Python **3.12.14**, uv **0.12.23** и GNU Make. Версия Python
задана в `.python-version`, версия uv проверяется в `pyproject.toml`.
При отсутствии uv его можно установить командой
`python -m pip install uv==0.12.23`.

Все команды ниже выполняются из каталога `backend/`:

```sh
make sync
make test
make lint
make types
make run
```

`make sync` устанавливает runtime- и dev-зависимости в `.venv` строго по
`uv.lock`, без изменения lockfile. Он фиксирует версии транзитивных
зависимостей и хэши артефактов PyPI. Для проверки установки с нуля можно
использовать отдельное пустое окружение:

```sh
UV_PROJECT_ENVIRONMENT=/tmp/akbrizcase-backend-clean uv sync --locked
```

`make run` запускает один процесс Uvicorn на `127.0.0.1:8000`, без reload,
с отключённым доверием к proxy headers. Эквивалентная команда:

```sh
uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Из другого терминала:

```sh
curl --fail-with-body http://127.0.0.1:8000/healthz
```

Ожидается HTTP 200 и `{"status":"ok"}`. Остановка сервера — Ctrl+C.
FastAPI также предоставляет стандартные `/docs`, `/redoc`, `/openapi.json`.

Настройки находятся в `app/core/config.py`: название приложения и выключенный
debug. Для liveness и unit-тестов переменные окружения, `.env`, пароли
и внешние сервисы не требуются; DB-конфигурация находится в `app/core/db.py`. Локальные `.env` и артефакты Python
исключены из git.

`make test` проверяет импорт приложения в отдельном процессе без
`DATABASE_URL` и HTTP `/healthz` через TestClient с ASGI lifespan.
`make lint` запускает Ruff (включая проверку форматирования), `make types` —
mypy в strict-режиме для приложения и тестов. Прямые зависимости закреплены
точными версиями в `pyproject.toml`; их обновление требует обновления
`uv.lock` и повторного выполнения проверок.

`/healthz` сообщает только о работе API-процесса. `/readyz` возвращает 200
только при доступной PostgreSQL 16 и совместимой схеме; без DB-конфигурации — 503.
Без конфигурации API по-прежнему импортируется и запускается. Для подключения
используются DB_HOST/PORT/NAME/USER и DB_PASSWORD_FILE; для миграции — отдельные
MIGRATION_DB_USER/PASSWORD_FILE. Паролей по умолчанию и автоматических миграций нет.

Полные команды migrate, настройки ролей/секретов, изолированные integration-тесты
и восстановление тестовой среды: [MVP-003](../docs/MVP-003.md).
Из корня: `make migrate`, `make integration`, `make integration-reset`.
Из backend: `make migrate` с MIGRATION_DB_PASSWORD_FILE.
Production readiness не заявляется.
