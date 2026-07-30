# Eurisko University Assistant

University assistant web app with a student portal and an admin panel. See
[CLAUDE.md](CLAUDE.md) for architecture decisions and the data model, and
[PROJECT_PLAN.md](PROJECT_PLAN.md) for the phase-by-phase build order.

**Current phase: 0 — repo & environment.**

## Layout

```
backend/     FastAPI + PydanticAI, managed with uv
  app/       application code (config, db, main)
  alembic/   migrations
frontend/    React (arrives in Phase 6)
ingestion/   PDF parsing / chunking / embedding (arrives in Phase 3)
data/        the three source files — the only things the assistant may know from
docker-compose.yml
```

## Running it

Requires Docker Desktop. From a clean clone, no `.env` needed:

```bash
docker compose up --build
```

That starts Postgres (with pgvector), applies migrations, and serves the API on
<http://localhost:8000>.

| URL | What it tells you |
|---|---|
| <http://localhost:8000/health> | the process is up |
| <http://localhost:8000/health/db> | `SELECT 1` reached Postgres, and pgvector is installed |
| <http://localhost:8000/docs> | Swagger UI |

To override the database credentials or ports, `cp .env.example .env` and edit.
`.env` is untracked and is also where the model API key goes in a later phase.

## Working on the backend outside Docker

```bash
cd backend
uv sync
DATABASE_URL=postgresql+psycopg://eurisko:eurisko_dev@localhost:5432/eurisko \
  uv run uvicorn app.main:app --reload
```

Migrations:

```bash
cd backend
uv run alembic upgrade head                       # apply
uv run alembic revision --autogenerate -m "..."   # create from model changes
```

The source directory is bind-mounted into the `backend` container, so edits
reload without a rebuild. Rebuild only when dependencies change:

```bash
docker compose up --build backend
```
