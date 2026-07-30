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

---

## Phase 2 — plain API endpoints, no agent

### These endpoints are not the student-facing surface

The plan specifies `/students/{id}/...`, and the cross-cutting check says the
authenticated student ID must never come from a client-supplied parameter. Both
are right: a path parameter is correct for the admin panel's browsers and wrong
for a student reading their own record.

So Phase 2 builds the by-ID surface, and every function in `records.py`,
`academics.py` and `eligibility.py` takes `student_id` as an argument and filters
on it *in SQL*. The scoping guarantee therefore holds wherever the ID came from,
and Phase 4's `/me/*` surface is a routing change rather than a rewrite. Recorded
in `CLAUDE.md` as a carry-forward so it cannot be forgotten: until `/me/*` exists,
no student-facing client may call `/students/{id}/*`.

### Handbook policy lives in config, not in the settings table

The C− prerequisite threshold (the one Phase 1 left homeless), the three-attempt
limit, the 9–15 full-time band, the 9-credit probation cap and the 3.00 overload
GPA are all in `config.Settings`, each next to the Handbook sentence it comes
from.

Deliberately *not* in `assistant_settings`: that table is the admin panel's
behaviour config (tone, model, length, temperature). Exposing degree rules through
the same form would make "wrong graduation answers" an admin-editable feature.

Note the two distinct C− rules, which are easy to conflate: the *global*
prerequisite gate lives in config, while whether a course counts toward Major Core
is `program_requirement_categories.min_grade_points`, per-category data.

### Eligibility explains itself

`check_eligibility` returns a list of `{rule, satisfied, detail}` rather than a
boolean. A bare refusal is useless to a student and unciteable by the agent, and
the same structure is what the Phase 5 tool will hand the model.

Rules are gathered in SQL and judged in Python, because each has to phrase itself
in prose. Blocking: offered this term, prerequisites at C− or above, attempt limit,
not already registered, load within cap. Conditional rather than blocking: a load
above 15 is *permitted* with advisor approval when GPA allows, so it surfaces as
`requires_advisor_approval` instead of a refusal.

An unknown course returns 404, not "ineligible" — "you are not eligible for a
course that does not exist" would be a confidently wrong answer.

### A bug the 5-student matrix caught

The first implementation computed prospective load as
`registered_credits + course.credits` unconditionally. For a course the student is
*already registered for*, those credits are already inside `registered_credits`,
so it overstated the total by the course's credits — Karim's MECH 310 read
"9 → 12" when registering again would change nothing, and Lynn's MATH 101 gained a
spurious load blocker on top of the real one.

Fixed by adding zero credits when already registered and skipping the load rule
entirely in that case. `verify_phase2.py` now asserts
`prospective == registered` whenever `already_registered`, so it cannot regress.
Worth recording because it is the kind of error that never fails loudly — it just
quietly produces a plausible wrong number.

### Verification goes through HTTP

Phase 1 already checked the SQL against an independent implementation, so
`verify_phase2.py` tests what is genuinely new: routing, response schemas,
serialisation, error paths. It asserts the GPA and credit figures hand-verified in
Phase 1, making it a regression guard — if a refactor changes how credits are
counted, those numbers move and the script fails.

It also checks two invariants that would otherwise be easy to break silently:
`schedule.total_credits` must equal `credits_in_progress` (they derive from the
same enrollments by different routes), and `credits_applied <= credits_required`
for every category, which is the capping rule stated as an assertion.

### Two results that look wrong and are not

- **Rania is refused every additional course.** She is registered for exactly 9
  credits and probation caps her at 9, so any addition breaks the cap. Correct,
  and the message says so rather than citing prerequisites.
- **Lynn cannot exceed 15 credits.** She is at 13; a fourth 3-credit course needs
  advisor approval *and* a GPA of 3.00+, and a first-term student has no GPA at
  all. Refusing is the literal rule; the message says "you have no GPA yet"
  instead of implying a low one.

### Still open after Phase 2

- ~~The prerequisite C− threshold has no home.~~ Resolved in Phase 2:
  `config.prerequisite_min_grade_points`.
- **Academic standing is stored, not derived.** It comes from the registrar as
  given. Rania's 1.65 is consistent with her recorded probation, but nothing
  *computes* standing, and the two-consecutive-review-terms dismissal rule (Summer
  excluded) is unimplemented. Fine while the two agree; a divergence would be
  invisible.
- **Withdrawals are unmodelled beyond the grading scale.** `W` is handled
  correctly in GPA and credits, but the max-4-W's-per-degree limit and the
  add/drop/withdraw deadlines are not checked anywhere.
- **`/me/*` does not exist yet** — see the Phase 2 note above; this is the one
  carry-forward that is a correctness issue rather than a gap.
