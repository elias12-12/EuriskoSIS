# Eurisko University Assistant

University assistant web app with a student portal and an admin panel. See
[CLAUDE.md](CLAUDE.md) for architecture decisions and the data model, and
[PROJECT_PLAN.md](PROJECT_PLAN.md) for the phase-by-phase build order.

**Current phase: 2 — plain API endpoints, no agent — complete.**

## Layout

```
backend/     FastAPI + PydanticAI, managed with uv
  app/       config, db, models (schema), main, schemas (responses)
             academics (GPA / degree progress), records (profile / schedule /
             history), eligibility (registration rules)
    routers/ students.py
  alembic/   migrations
  scripts/   load_spreadsheet.py, verify_phase1.py, verify_phase2.py
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

## Endpoints

Browse them in Swagger at <http://localhost:8000/docs>.

| Endpoint | Returns |
|---|---|
| `GET /students/{id}/profile` | identity, programme, advisor, standing, GPA |
| `GET /students/{id}/schedule` | current-term timetable (days, times, rooms, staff) |
| `GET /students/{id}/courses` | full history, newest term first, in-progress included |
| `GET /students/{id}/degree-progress` | per-category earned vs required, capped |
| `GET /students/{id}/eligibility/{course_code}` | may they register, and why not |

Course codes contain a space — URL-encode them: `.../eligibility/MECH%20310`.

> **These are not the student-facing surface.** The ID is a path parameter, which
> suits the admin browsers but not a student reading their own record. Phase 4 adds
> `/me/*` taking the ID from the authenticated session. Until then, no
> student-facing client may call `/students/{id}/*`.

Check them all against all five students:

```bash
docker compose exec backend python scripts/verify_phase2.py
```

Five endpoints x five students, a seven-course eligibility matrix, and the 404
paths. It asserts the GPA and credit figures hand-verified in Phase 1, so it
doubles as a regression guard.

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
