# Backend — MVP-001

Минимальный FastAPI-каркас: только `GET /healthz`, без БД и бизнес-модулей.

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
debug. Переменные окружения, `.env`, пароли и внешние сервисы не требуются;
конфигурация не загружает секреты. Локальные `.env` и артефакты Python
исключены из git.

`make test` проверяет импорт приложения в отдельном процессе без
`DATABASE_URL` и HTTP `/healthz` через TestClient с ASGI lifespan.
`make lint` запускает Ruff (включая проверку форматирования), `make types` —
mypy в strict-режиме для приложения и тестов. Прямые зависимости закреплены
точными версиями в `pyproject.toml`; их обновление требует обновления
`uv.lock` и повторного выполнения проверок.

`/healthz` сообщает только о работе API-процесса. Readiness с проверкой БД
относится к MVP-003. Frontend, Docker/Compose/Nginx, worker, CI, миграции
и бизнес-логика не входят в этот каркас; готовность production не заявляется.
