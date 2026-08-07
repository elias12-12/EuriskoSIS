# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv is pinned to the version that produced uv.lock, so the container resolves
# dependencies exactly the way the host did.
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

# The venv lives OUTSIDE /app on purpose. Compose bind-mounts ./backend over
# /app for --reload, which would hide a venv inside it; the usual fix is an
# anonymous volume at /app/.venv, but anonymous volumes survive `up --build`, so
# the venv from the first-ever build keeps shadowing every later one. That is a
# ModuleNotFoundError for any dependency added after day one, and it looks like
# a broken image rather than a stale mount. Putting it at /opt/venv means the
# bind mount cannot reach it and there is nothing to shadow.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Paths are relative to the repo root, not to backend/: the build context widened
# in Phase 3 so the image can carry `ingestion/` as well. See docker-compose.yml.

# Dependencies first, as their own layer: this is only rebuilt when pyproject.toml
# or uv.lock change, not on every source edit.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project

COPY backend/ ./

# Landed inside /app so it is importable as `ingestion` under the existing
# PYTHONPATH=/app, with no second entry to keep in sync. It stays a top-level
# directory in the repo (the Phase 0 layout); this is only where it lives in the
# container. Compose mounts the host copy over this path for --reload.
COPY ingestion/ ./ingestion/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
