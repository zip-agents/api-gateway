# syntax=docker/dockerfile:1
# `docker build .` produces the runtime image (the last stage). `--target dev` produces the
# image that runs lint, type checks and tests, so the host needs nothing but Docker.

ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.1

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS base
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# --- runtime dependencies only ------------------------------------------------------------
FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

# --- lint, types and tests -----------------------------------------------------------------
FROM base AS dev
COPY pyproject.toml uv.lock ./
RUN uv sync --locked
ENV PATH="/app/.venv/bin:${PATH}"
COPY gateway ./gateway
COPY docs ./docs
COPY tests ./tests
CMD ["sh", "-c", "ruff check . && ruff format --check . && mypy && pytest"]

# --- runtime: non-root, works with a read-only root filesystem -------------------------------
FROM ${PYTHON_IMAGE} AS runtime
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin gateway
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY gateway ./gateway
COPY docs ./docs
RUN /app/.venv/bin/python -m compileall -q gateway
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health/live' % os.environ.get('PORT', '8000'), timeout=2)"]
STOPSIGNAL SIGTERM
CMD ["python", "-m", "gateway"]
