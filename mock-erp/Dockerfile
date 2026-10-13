# syntax=docker/dockerfile:1

# ---- Builder: resolve the locked dependencies into /app/.venv with uv ----
FROM python:3.13.16-alpine3.24 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /bin/uv

# Use the image's Python, compile bytecode once here, copy out of the cache mount
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, so source edits do not reinstall them
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

# Then the project itself (editable, so the venv points at /app/src)
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- Runtime: the venv, the source and the migrations; no uv, no build tools ----
FROM python:3.13.16-alpine3.24

RUN addgroup -S -g 10001 erp && adduser -S -D -H -u 10001 -G erp erp

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=builder --chown=root:root /app/.venv /app/.venv
COPY --chown=root:root src ./src
COPY --chown=root:root alembic.ini ./
COPY --chown=root:root migrations ./migrations
COPY --chown=root:root --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

USER erp
EXPOSE 8090

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
