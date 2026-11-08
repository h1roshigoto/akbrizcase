# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.12.14-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:$PATH"
# Debian security versions pinned; no daemon, freshclam, or bundled signatures.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       clamav=1.4.6+dfsg-0+deb12u1 clamav-base=1.4.6+dfsg-0+deb12u1 \
       libclamav12=1.4.6+dfsg-0+deb12u1 libseccomp2=2.5.4-1+deb12u1 \
    && rm -rf /var/lib/apt/lists/* /var/lib/clamav/*
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY --chmod=0444 pyproject.toml uv.lock ./
# Optional session CA stays in a BuildKit mount, never in an image layer.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --locked --no-dev --no-install-project
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /var/lib/akbrizcase/files \
    && chown app:app /var/lib/akbrizcase/files \
    && chmod 0700 /var/lib/akbrizcase/files
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN chmod -R a+rX /app/app /app/migrations /app/alembic.ini
USER 10001:10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log"]

# Disposable integration runner; the default final image remains runtime-only.
FROM runtime AS integration
USER root
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --locked --no-install-project
COPY tests ./tests
COPY kb_acceptance ./kb_acceptance
RUN chmod -R a+rX /app/tests /app/kb_acceptance
USER 10001:10001
CMD ["python", "-m", "pytest", "-m", "integration", "-p", "no:cacheprovider", "-v"]

FROM runtime AS final

# One-shot synthetic drill. Independent build graph avoids copying each large
# application layer through Docker VFS on small disposable runners.
FROM postgres:16.13-bookworm AS pg16tools
FROM python:3.12.14-slim-bookworm AS backup
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=pg16tools /usr/lib/postgresql/16/bin/pg_dump /usr/lib/postgresql/16/bin/pg_restore /usr/local/bin/
COPY . /app
RUN --mount=type=secret,id=proxy_ca \
    apt-get update && apt-get install -y --no-install-recommends \
      libpq5 clamav=1.4.6+dfsg-0+deb12u1 clamav-base=1.4.6+dfsg-0+deb12u1 \
      libclamav12=1.4.6+dfsg-0+deb12u1 libseccomp2=2.5.4-1+deb12u1 \
    && rm -rf /var/lib/apt/lists/* /var/lib/clamav/* \
    && if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --locked --no-install-project \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /var/lib/akbrizcase/files /var/lib/akbrizcase/restore \
       /var/lib/akbrizcase/store /var/lib/akbrizcase/journal /var/lib/akbrizcase/blocked \
    && chown -R app:app /var/lib/akbrizcase && chmod 0700 /var/lib/akbrizcase/* \
    && chmod -R a+rX /app
USER 10001:10001
CMD ["python", "-m", "app.backup", "--help"]

# Preserve the ordinary default target used by Compose/release jobs.
FROM runtime AS application
