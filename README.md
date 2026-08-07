# Eurisko University Assistant

A university assistant with two surfaces: a **student portal** where a student
signs in with their ID, sees their own record, and chats with an assistant; and
an **admin panel** for managing the source documents, browsing the data, and
configuring the assistant's behaviour without touching code.

The assistant answers three kinds of question and knows which is which —
**document** questions (policy, deadlines, fees, course descriptions) answered
only from retrieved passages and always cited; **personal** questions (my
schedule, my grades, my progress) scoped strictly to the logged-in student; and
**hybrid** questions like *"Am I allowed to register for MECH 310?"*, which need
the Handbook's rules and that student's transcript at once.

- [DESIGN.md](DESIGN.md) — the architecture and why (start here)
- [DESIGN_NOTES.md](DESIGN_NOTES.md) — the phase-by-phase working record
- [CLAUDE.md](CLAUDE.md) — locked decisions, data model, current status
- [PROJECT_PLAN.md](PROJECT_PLAN.md) — build order and exit checks

## Layout

```
backend/          FastAPI + PydanticAI, managed with uv
  app/            config, db, models, main, schemas
                  academics (GPA / degree progress), records, eligibility
                  retrieval (vector search), agent (tools), chat, conversations
                  auth (the only producer of an authenticated identity)
                  assistant_config, appointments, browse
    routers/      auth, me, students, documents, admin
  alembic/        migrations 0001-0007
  scripts/        loader, ingestion, and one verify_phaseN.py per phase
frontend/         React 19 + Vite + TypeScript; student portal and admin panel
ingestion/        PDF parsing, chunking, embedding (importable by the backend)
data/             the three source files — the only things the assistant may know from
docker-compose.yml
```

## Running it

Requires Docker Desktop.

```bash
cp .env.example .env      # then set OPENAI_API_KEY
docker compose up --build
```

| URL | What |
|---|---|
| <http://localhost:5173> | student portal and admin panel |
| <http://localhost:8000/docs> | Swagger UI |
| <http://localhost:8000/health/db> | Postgres reachable, pgvector installed |

A clean clone runs without `.env` — Compose supplies throwaway dev defaults for
the database and the admin password. It is needed for `OPENAI_API_KEY`, which
has no default because it is a real secret. Without it every record endpoint and
the whole portal still work; document search returns 503 and chat cannot run.

Then load the spreadsheet and ingest the PDFs:

```bash
docker compose exec backend python scripts/load_spreadsheet.py
docker compose exec backend python scripts/ingest_documents.py
```

Both are re-runnable. The loader wipes and reinserts the tables it owns;
ingestion is delete-and-reinsert per document and skips files whose bytes are
unchanged (`--force` after changing a chunker).

Sign in to the portal with any of the five student IDs — `S2023011`,
`S2023027`, `S2024019`, `S2025008`, `S2026042` — or to the admin panel with
`ADMIN_PASSWORD` (default `eurisko_admin`).

## Verifying it

One script per phase. Each fails loudly and exits non-zero.

```bash
docker compose exec backend python scripts/verify_phase1.py              # GPA / progress vs pandas
docker compose exec backend python scripts/verify_phase2.py              # 5 endpoints x 5 students
docker compose exec backend python scripts/inspect_chunks.py --summary   # chunk boundaries, no key needed
docker compose exec backend python scripts/verify_phase3.py              # the six retrieval questions
docker compose exec backend python scripts/verify_phase4.py              # cross-student scoping
docker compose exec backend python scripts/verify_phase5.py              # MECH 310, and no auto-booking
docker compose exec backend python scripts/verify_phase6.py              # admin, and live settings
```

Several take a flag that drops the parts needing an API key or a database, so
the structural half can run anywhere:

```bash
scripts/verify_phase4.py --structural    # tool schemas only — no key, model or database
scripts/verify_phase5.py --gates         # the human-in-the-loop gates — needs nothing
scripts/verify_phase6.py --structural    # needs the database, but no key
```

That split is deliberate: the scoping and human-in-the-loop guarantees are
properties of the *schemas and gates*, not of the model's behaviour, so they are
asserted directly. A well-behaved model would otherwise hide a broken control.

> **Status:** everything checkable without an API key has been run and passes.
> The exit checks that need a key or a live database have not — see
> [CLAUDE.md](CLAUDE.md) for the current state.

## API surface

Browse it in Swagger. Three groups, and the distinction matters:

| Group | Identity comes from | For |
|---|---|---|
| `/me/*` | the authenticated session | **the student's browser** |
| `/students/{id}/*` | a path parameter | the admin panel's browsers only |
| `/admin/*` | an administrator session | the admin panel |

`/me/profile`, `/me/schedule`, `/me/courses`, `/me/degree-progress`,
`/me/eligibility/{course}`, `/me/appointments`, `POST /me/chat`. None of them
takes a student ID in any position — that is the point.

Course codes contain a space, so URL-encode them: `.../eligibility/MECH%20310`.

## Working outside Docker

```bash
cd backend
uv sync
DATABASE_URL=postgresql+psycopg://eurisko:eurisko_dev@localhost:5432/eurisko \
ADMIN_PASSWORD=eurisko_admin DATA_DIR=../data \
  uv run uvicorn app.main:app --reload

uv run alembic upgrade head                       # apply migrations
uv run alembic revision --autogenerate -m "..."   # create from model changes
```

```bash
cd frontend
npm install
npm run dev        # :5173, expects the API on :8000
npm run build      # tsc -b && vite build
```

Both source trees are bind-mounted into their containers, so edits reload
without a rebuild. Rebuild only when dependencies change:

```bash
docker compose up --build backend frontend
```
