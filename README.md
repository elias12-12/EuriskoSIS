# Eurisko University Assistant

University assistant web app with a student portal and an admin panel. See
[CLAUDE.md](CLAUDE.md) for architecture decisions and the data model, and
[PROJECT_PLAN.md](PROJECT_PLAN.md) for the phase-by-phase build order.

**Current phase: 1 — data modelling and loading — complete.**

## Layout

```
backend/     FastAPI + PydanticAI, managed with uv
  app/       config, db, models (schema), academics (GPA / degree progress), main
  alembic/   migrations
  scripts/   load_spreadsheet.py, verify_phase1.py
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

## Loading the data

Migrations run automatically on container start; the data load is explicit:

```bash
docker compose exec backend python scripts/load_spreadsheet.py
```

Re-runnable — it wipes the tables it owns and reinserts, so run it again after any
schema change. The dataset is mounted read-only at `/data`.

Then check the academic calculations against an independent implementation:

```bash
docker compose exec backend python scripts/verify_phase1.py
```

This recomputes GPA, credits and every requirement category for all five students
straight from the spreadsheet in pandas and diffs it against the SQL, then proves
credit capping and the repeat rule against a fabricated student inside a
rolled-back transaction. Exits non-zero on any disagreement. Run it after
touching anything in `app/academics.py`.

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
