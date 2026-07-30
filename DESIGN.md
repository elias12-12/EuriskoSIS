# Design notes

Running notes, written as we go (per PROJECT_PLAN.md Phase 7 — the reasoning is
impossible to reconstruct afterwards). Distilled into the final 1–2 pages last.

Must eventually answer: database choice and why, chunking/retrieval strategy and
why, what we cached and why, what we'd do differently with two more weeks.

---

## Phase 0 — repo & environment

### Database: one PostgreSQL instance with pgvector

Locked in `CLAUDE.md` §3. The alternative was a dedicated vector store beside a
relational database. Rejected for now because every scoping rule this app has to
enforce is relational — an embedding chunk is not useful without the document row
that gives it a citation, and a student's transcript is the thing that makes an
eligibility answer correct. Two stores would mean two connection pools, no
foreign keys across the boundary, and application-level joins to reassemble
what one `JOIN` already does. Revisit only if a concrete query pattern proves it
wrong, not because a vector DB is conventional.

### Migrations: Alembic (decided Phase 0)

The `[DECIDE]` in `CLAUDE.md` §3 between Alembic and hand-written SQL scripts.
Chose Alembic:

- SQLAlchemy is already a required dependency, so the models are the natural
  source of truth and `--autogenerate` keeps DDL from drifting from them by hand.
- The Phase 1 schema will churn (that phase is explicitly the one that
  "separates projects"), and versioned up/down revisions make that churn cheap.
- Migrations run automatically on container start, so a fresh clone never has a
  "did you remember to apply the schema?" step.

Cost accepted: pgvector's `VECTOR` type needs help from Alembic. `alembic/env.py`
imports `pgvector.sqlalchemy` to register the type so autogenerate compares
embedding columns instead of proposing to recreate them every run, and
`script.py.mako` imports it too so generated revisions can name it.

`CREATE EXTENSION vector` lives in revision `0001` rather than relying on the
`pgvector/pgvector:pg16` image's setup, so any database this app migrates ends up
correct — not only ones created by `docker compose up`.

### Health endpoints are split

`/health` is liveness (the process answers). `/health/db` is readiness and also
reports the pgvector extension version. "Postgres is unreachable" and "the
extension the whole retrieval layer depends on is missing" are different
failures with different fixes, and distinguishing them costs one query now
versus a confusing Phase 3 debugging session later.

### Config comes from the environment, with no `database_url` default

`Settings.database_url` is required. A default would let the app quietly connect
to a developer's local Postgres when the container URL was misconfigured, which
looks like success and isn't. Compose supplies throwaway dev defaults for the
Postgres credentials inline so a clean clone runs with no `.env`; real secrets
(model API keys) only ever come from the untracked `.env`.

### Still open

- **Embeddings** — `CLAUDE.md` §3 still marks this `[DECIDE]` (OpenAI
  `text-embedding-3-small` vs. local sentence-transformers). Not needed until
  Phase 3; deferred deliberately so the choice can be made against the actual
  chunking strategy rather than in the abstract.
