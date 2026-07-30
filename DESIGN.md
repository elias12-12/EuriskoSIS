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

### Embeddings: OpenAI `text-embedding-3-small` (decided before Phase 3)

The other `[DECIDE]` from `CLAUDE.md` §3, deliberately deferred out of Phase 0 so
it could be made against the real corpus rather than in the abstract.

Chosen at the default **1536 dimensions**, which fixes
`document_chunks.embedding` as `VECTOR(1536)`.

The deciding factor is scale: the corpus is two PDFs totalling 11 pages. Whole-
corpus ingestion is a handful of API calls and a query costs one embedding, so
there is no volume here that would repay self-hosting. Keeping it hosted also
keeps `sentence-transformers` and torch — roughly 2GB of image — out of the
backend container, and avoids CPU inference competing with the API process for
resources in a single-container dev setup.

Accepted trade-off: ingestion and retrieval now require `OPENAI_API_KEY` and
network access, so retrieval does not work offline. This makes `.env` load-bearing
for the first time — until now Compose's inline dev defaults covered everything.

Not a one-way door: switching to a local model later means re-embedding ~11 pages
plus one migration to change the vector dimension. Worth stating explicitly,
because the usual argument against a hosted embedding model is lock-in, and at
this corpus size that argument does not hold.

---

## Phase 1 — data modelling and loading

### `selection_rule` is derived, not transcribed

`Program_Requirements` gives `credits_required` per category but says nothing
about whether every course is required or only some — that rule is prose in the
Catalogue. Rather than hand-transcribing five rules per programme, the loader
derives it from credits: if a category's courses carry exactly `credits_required`
credits, all are required; if they carry more, it is a choose-N category with
N = `credits_required` / per-course credits.

This holds for all ten categories (Core 28=28 → ALL; Gen Ed 12>9 → any 3;
Major 18=18 → ALL; Prof 8=8 → ALL; Electives 12>9 → any 3). The derivation
consults no category or programme name, which is what lets one query serve both
programmes. It refuses to guess where it cannot: mixed per-course credits in a
choose-N category raise rather than inventing an N.

The loader then cross-checks the derived rules against the Catalogue's stated
ones, keyed by category_id suffix (`CORE`/`GEN`/`MAJ`/`PROF`/`ELEC`) so the check
is itself programme-independent. Derivation and verification from two different
sources, failing loudly on disagreement, beats either alone.

### `min_grade_points` as a column, not a branch

The Handbook requires C− or above for a course to count toward **Major Core**
specifically, while a D earns credit everywhere else. Encoding that as
`if category_name == 'Major Core'` is precisely what CLAUDE.md §5's
non-negotiable rule forbids, so it is a nullable `min_grade_points` column on
`program_requirement_categories`, seeded from Handbook policy and applied in SQL
as `(min_grade_points IS NULL OR grade_points >= min_grade_points)`.

A `P` fails this gate deliberately: a pass carries no grade points and so does
not evidence C− or above.

### One satisfaction test for both selection rules

`is_satisfied` is `credits_counted >= credits_required` for every category, with
no branch on `selection_rule`. That works because an ALL category's courses sum
to exactly its requirement, so reaching the credit total is only possible with
every course — the same comparison that means "any 3 of 4" elsewhere. The rule
still lives in the schema for the eligibility and UI work that needs to *explain*
a category, not merely score it.

### Capping is enforced in SQL, and both rules are proven synthetically

Credits applied to a category are `LEAST(credits_counted, credits_required)`, so
surplus can never offset a shortfall elsewhere. `credits_counted` is kept
alongside so the surplus stays visible instead of being silently discarded.

Two rules the five students **cannot** validate:

- no student has completed more courses than an ANY_N category requires, so
  nothing exercises the cap;
- no student has *passed* a course twice — Rania's two repeats are still in
  progress, so each has exactly one completed attempt, and nothing exercises
  "only the higher grade counts" or "credit earned once".

Both would therefore pass the 5-student matrix even if implemented wrongly. So
`scripts/verify_phase1.py` fabricates a student who exercises both (all four Gen
Ed courses passed; MATH 101 passed at C then repeated at A), checks the answers,
and rolls back inside a nested transaction. The frozen dataset is never written
to.

### Verification is a second implementation, not a restated query

`scripts/verify_phase1.py` recomputes GPA, credits and every category from the
spreadsheet in pandas — dict-and-loop rather than set-based SQL, reading the
workbook rather than the database, and restating the Handbook policy from the
documents rather than reading `min_grade_points` out of the table it is meant to
be checking. A shared mistake is therefore unlikely, and it also catches a loader
that seeded policy wrongly. Hand-checked anchors: Maya 231.40/64 = **3.62**,
Jad 153.00/55 = **2.78** with 55 credits across CORE 28 / ELEC 9 / GEN 3 / MAJ 15
/ PROF 0.

### Why Jad is the trap, precisely

Not that a credit total and a per-category check disagree about MET vs not-met —
no student is at 72 yet, so that comparison agrees trivially for all five and
proves nothing. The trap is that a headline number hides *which* requirements
remain: Jad reads as 76% complete (55/72) while holding 1 of 4 Gen Ed courses and
having never started the 8-credit capstone category. Any answer built on the
percentage is confidently wrong about how far he is from graduating.

### Modelling decisions worth recording

- **`expected_graduation_term` is not a foreign key**, unlike `entry_term`. Its
  values (SP2027, SP2028, SP2030) are future terms absent from the `Terms` sheet,
  which ends at FA2026. Seeding the missing rows would mean fabricating start and
  end dates, and a fabricated date is exactly what the assistant must never state
  as fact. CLAUDE.md §5 permits adjusting constraints while keeping the shape.
- **`enrollments` keyed on (student_id, term_code, course_code)** — verified
  unique in the source. This is what makes repeats representable at all: same
  course, different term.
- **`grade_points` is `NUMERIC`, not float.** GPA is a ratio of sums over these,
  and the C− threshold is an exact comparison against 1.70; a float 1.7 that is
  really 1.6999999 would decide prerequisite eligibility wrongly.
- **`grade` is nullable and FK'd to `grading_scale`**, with a CHECK tying NULL
  grade to 'In progress'. In-progress rows are the reason credit and GPA queries
  must filter through the grade join rather than counting enrollment rows.
- **`class_schedule` carries no `course_title`/`credits`** even though the sheet
  does; they duplicated `courses` exactly, and two sources for one fact is one
  too many.
- **Current term is configuration** (`CURRENT_TERM`, default FA2026), not a
  literal in queries. The sheet name encodes the term and nothing else does.
- **No indexes beyond the primary keys yet.** 84 enrollments and 33 courses make
  every access a trivial scan; adding indexes now would be guessing at Phase 2's
  query shapes. Revisit when there is a real query plan to improve.

### Still open after Phase 1

- The prerequisite C− threshold is a *global* Handbook rule (as opposed to the
  per-category gate above) and has no home yet. Phase 5 builds eligibility; the
  threshold should land as config or a policy row then, not as a literal.
- Academic standing is stored as given by the registrar, not derived from GPA.
  Rania's 1.65 is consistent with her recorded probation, but nothing yet
  *computes* standing or the two-consecutive-terms dismissal rule.
