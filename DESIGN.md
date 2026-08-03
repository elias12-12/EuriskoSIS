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

---

## Phase 3 — document ingestion and retrieval

### Chunking follows the documents' own structure, not a token budget

Neither chunker has a chunk size. Both documents already state where one idea
ends and the next begins — the Catalogue through its course entries and
requirement tables, the Handbook through its numbered sections — and a
fixed-window chunker would ignore that in favour of a number chosen by nobody.

The two strategies, and what each is protecting:

**Catalogue → one chunk per course (33), per programme (2), plus overview.**
The invariant is that a course is never separated from its prerequisite line.
A 400-token window over page 3 would routinely end mid-entry, putting one
course's `Prerequisite:` line in the same chunk as the *next* course's title —
so "What are the prerequisites for CENG 320?" retrieves a passage that contains
the string "CENG 320" and a prerequisite line belonging to CENG 330. That is not
a near miss; it is a confident wrong answer, which CLAUDE.md §7 rule 4 calls the
worst outcome. `catalogue.py` asserts the count is 33 and that every course chunk
still contains a prerequisite line, so the invariant cannot regress silently.

**Handbook → one chunk per numbered (sub)section (18), tables intact.**
Following the numbering keeps every table whole for free, because each lives
entirely inside one subsection. It also produces the citation as a by-product:
`section_ref` is the document's own "2.3", not an id we invented, so a citation
is checkable by a human holding the PDF.

### Extraction is a page-tagged line stream, not a list of pages

Handbook sections 6 (Tuition and Fees) and 8 (Student Services) each run across a
page boundary in the middle of a table. Chunking page by page would split both —
including the services directory, one row short of the routing table it feeds.
So `extract.py` flattens both documents into one stream of lines that each carry
their page number, and a chunk records the page of its *first* line. Verified by
inspection: sections 6 and 8 come out as single chunks starting on pages 4 and 5
and running onto the next.

Running headers are removed by matching the expected four-line block and raising
if it is not there, rather than dropping the first four lines. A header left in
would end up embedded and quoted back in an answer; four lines dropped blindly
would delete real content the first time a page was laid out differently.

### pypdf, and what it does not do

Both PDFs are digitally generated with a clean text layer, so `pypdf` recovers
them exactly — no OCR, and correct reading order inside tables. Checked page by
page before the chunkers were written.

What it does *not* do is recover cell structure. A table arrives as one cell per
line in row-major order, so a routing-table row becomes three consecutive lines
(enquiry, office, contact). That is contiguous and correct, and legible enough to
embed and to answer from, but it is flattened. Two visible artefacts: the grading
scale in 1.1 is a six-column table read as `A / 4.0 / C+ / 2.3 / W / …`, and
tables continuing across a page repeat their header row inside the chunk. Both
were left alone — stripping a repeated header risks removing real content, and
the pairs in 1.1 are recoverable as read.

Accepted with an escape route: if a table question ever fails the test set, the
fix is a structured table extractor (`pdfplumber`), not a re-chunk. Not done
pre-emptively, because it is a second parsing dependency bought against a problem
that has not appeared.

### The heading is inside the chunk, not only beside it

Every chunk opens with a context line naming the document and section, and
subsection chunks replay their parent section heading. `Prerequisite: MECH 210`
embeds to nearly nothing on its own; under `MECH 310 Fluid Mechanics` it answers a
question. Course chunks additionally carry their subject heading, which is what
makes a query phrased by topic rather than by code land in the right place.

This costs a few tokens per chunk and buys most of the retrieval precision. It is
also why `section_ref`/`page` are fields on the chunk type rather than metadata
attached later: a chunk that cannot cite itself is unusable under §7 rule 5, so
it should not be constructible.

### Section 5 keeps both term calendars in one chunk

Splitting Fall 2026 from Spring 2027 would sharpen retrieval slightly. It was
rejected: "the last day to drop a course without a W" has a *different answer in
each term*, and a chunk holding only one of them invites an answer that is
precisely wrong for the term the student meant. One chunk holding both forces the
answer to name the term. This is the one place where a deliberately coarser chunk
is the safer one.

### No vector index, and no hybrid search — yet

58 chunks. An exact sequential scan beats an approximate index at this size and,
unlike IVFFlat, cannot miss the true nearest neighbour, so `document_chunks`
carries no index on `embedding`. Same reasoning as Phase 1's "no indexes beyond
the primary keys": revisit when there is a real query plan to improve.

Likewise the retrieval is pure vector search with no lexical channel. Adding
Postgres full-text alongside it is the obvious upgrade if an exact-code query
like "CENG 320" ranks a neighbouring course first — but that is a hypothesis, and
the six-question test set is what decides it. Building the hybrid first would
mean never learning whether it was needed.

### Ingestion is re-runnable, and fails without leaving a half-corpus

Keyed on `documents.filename`, delete-and-reinsert, so the admin panel's
"re-run ingestion" button has something real to call. Three properties chosen
rather than fallen into:

- **A skip when the bytes are unchanged.** `sha256` on the document row makes
  re-running free, which is what allows the button to be pressed without thought.
  `--force` covers the case the hash misses: the file is identical but a chunker
  changed.
- **Failure leaves no chunks but does leave a record.** Chunk writes happen in
  one transaction rolled back on any error; the `documents` row is then updated to
  `failed` with the message in a *second* transaction, so the diagnosis survives
  the rollback. A half-ingested document that still answers questions would cite a
  section it no longer fully contains.
- **Retrieval filters on `status = 'ready'`**, so the window between deleting old
  chunks and committing new ones cannot serve a partially rebuilt corpus.

`corpus_status` reports `chunk_count` and `embedded_count` separately, because
"58 chunks, 0 searchable" is a real state and a single total would hide it.

### Verification is split in two, and the reason matters

`scripts/inspect_chunks.py` prints every chunk and needs neither a database nor an
API key. `scripts/verify_phase3.py` runs the six-question test set and needs both.

They are separate because **chunk boundaries are the one Phase 3 decision no later
test can catch**. A wrong boundary does not raise; it produces a passage that
retrieves well and answers badly. So the boundaries get read by eye, once, in
full, and the retrieval test is asked a different question.

The test set asserts the *expected section*, not merely that something plausible
came back. "Retrieval returned something about grading" is not a pass when the
question was how the GPA is calculated: 1.1 is the scale, 1.2 is the formula, and
only one of them answers it. Same trap in the calendar case — 2.3 gives the
add/drop *rule*, section 5 gives the *date*, and only the date is an answer.

### The API key is now genuinely load-bearing

Predicted in the Phase 0 note above; here it arrived. `OPENAI_API_KEY` is optional
in `Settings` and its absence is not a startup error — every Phase 2 endpoint
works without it. It fails at the first embedding call, through a dedicated
`MissingAPIKey` exception that the endpoint turns into a 503 rather than a 500,
because "not configured" and "the API failed" have different fixes and a generic
error sends people hunting for a bug.

### Repo layout: the build context widened

`/ingestion` stays a top-level directory (the Phase 0 layout), but the backend
image now builds from the repo root and copies it to `/app/ingestion`, importable
as `ingestion` under the existing `PYTHONPATH`. Compose mounts the host copy over
that path for `--reload`, the same nested-mount trick already used for `.venv`.

The alternative — running ingestion only from the host — was rejected because
Phase 6's re-ingest button needs to call `pipeline.ingest_all` in-process, and
discovering that after building the UI would mean a rewrite.

### Still open after Phase 3

- **The six-question test set has not been run.** Everything up to the embedding
  call is built and hand-verified; the exit check itself needs `OPENAI_API_KEY`
  and a running database, neither of which was available. Until it passes, Phase 3
  is not closed.
- **No lexical/hybrid channel**, by choice — see above. The test set decides.
- **Table structure is flattened**, not parsed — see the pypdf note.
- **Nothing re-ingests automatically.** Ingestion is a script; the admin panel
  endpoint that calls it is Phase 6 work, and `pipeline.ingest_all` is shaped for
  it but not yet wired to a route.

---

## Phase 4 — the agent

### The scoping guarantee is structural, not a prompt

This is the phase's whole point, so it is worth being precise about what
actually enforces it.

**No scoped tool has a parameter that could name a student.** `get_my_schedule`,
`get_my_courses` and `get_my_degree_progress` take no arguments at all. The JSON
schema the model receives is literally `{"properties": {},
"additionalProperties": false}`. There is no field to fill in, so there is
nothing for a user to talk the model into filling. A prompt injection cannot
supply an argument that does not exist in the signature.

The student ID reaches the tools through `StudentContext`, PydanticAI's `deps`
object, which the model never sees. It is built in the route handler from
`auth.current_student` — deliberately the *only* function in the application that
produces an authenticated student ID. If a second source ever appears, the
guarantee stops being checkable by reading one file.

The system prompt does tell the model to refuse cross-student questions. That is
a **courtesy**: it produces a good refusal message instead of a confused one. It
is not the control. Delete it and the model still cannot reach another student's
record.

This distinction is why `verify_phase4.py` is in two parts. Part 1 asserts the
schemas and needs no key, model or database; part 2 exercises two live sessions
and checks the quality of the refusal. **A test that only checked the model's
behaviour would be testing the courtesy while leaving the control unverified** —
and it would pass on a sufficiently well-behaved model even if the tools took a
`student_id` argument.

### `/me/*` closes the Phase 2 carry-forward

`CLAUDE.md` recorded this as a correctness issue rather than a gap, and it is now
fixed: `/me/profile`, `/me/schedule`, `/me/courses`, `/me/degree-progress`,
`/me/eligibility/{code}` take the ID from the session and expose no path, query
or body parameter naming a student.

Phase 2's decision to make every function in `records.py`/`academics.py`/
`eligibility.py` take `student_id` as an argument and filter on it *in SQL* paid
off exactly as predicted: this router is a routing change, not a second
implementation. `verify_phase4.py` asserts `/me/X` and `/students/{id}/X` return
byte-identical JSON, so the two surfaces cannot drift into disagreeing about a
record.

`/students/{id}/*` stays, for the admin panel's browsers. It now carries a
docstring saying so.

### Auth: a real login for an identity with no secret

The brief allows a student ID as sufficient identity. That makes login trivial
and makes it easy to skip the part that is not: something has to carry the
authenticated identity from the HTTP request to the data layer.

`POST /auth/login` exchanges an ID for an opaque session token, stored SHA-256
hashed in `student_sessions`. Considered and rejected: trusting an
`X-Student-Id` header. With ID-only login both are equally forgeable, so the
token buys no secrecy — what it buys is that the identity has exactly **one
shape and one origin**, which is what makes "the model can never supply the
student ID" a fact about the code rather than a promise about prompts.

`student_sessions` is not in CLAUDE.md §5's schema and is a deliberate addition;
§7 rule 2 requires an authenticated ID, and that has to live somewhere. Hashing
costs one line and means a database dump does not hand over live sessions.

Login returns 404 for an unknown student rather than a vague 401. There is no
secret to be wrong about, and vagueness here would be security theatre.

### Session memory stores each turn twice, on purpose

`messages.role`/`content` is the human-readable projection the Phase 6 chat panel
renders. `messages.model_message` is the serialised PydanticAI `ModelMessage`,
replayed verbatim as history.

Both exist because the display projection is lossy: it drops tool calls and their
results. Replaying only the prose would leave the model unable to see what it
already looked up, and "what about next term?" — the exact requirement in §7 rule
3 — depends on seeing it. They are written in one transaction from one run, so
they cannot disagree about what happened.

**A conversation is student-scoped data.** `load_for_student` refuses a thread
belonging to someone else, because a transcript contains grades, schedules and
degree progress — handing one over because a client passed a different
`conversation_id` would defeat every other scoping rule in the app. This was the
one place session memory could quietly become a hole, so the check lives in
`conversations.py` rather than in the route. Not-found and not-yours return the
same 404: distinguishing them confirms that a given id exists and belongs to
somebody.

### Behaviour config is read per request, and holds no policy

`assistant_config.load` runs on every chat request and is deliberately **not**
cached, unlike `config.get_settings()` which is `@lru_cache`d. The difference is
that one reads immutable environment config and the other reads a mutable table;
caching the latter would break the single behaviour the admin panel exists to
demonstrate (Phase 6: changing a setting changes the *very next* response).

`tone` and `response_length` are constrained vocabularies in the schema because
each maps to a specific instruction fragment. An unrecognised value would
contribute nothing, which is indistinguishable from the setting having no effect
— the worst kind of bug to debug through an LLM.

The fixed behaviour rules (grounding, citation, scoping, uncertainty, "never
recompute GPA yourself") are concatenated **before** the admin's tone and length
fragments, so no combination of settings can read as overriding them. And no
academic policy lives in this table — the Handbook rules stay in
`config.Settings`, because an admin editing degree rules through a tone-and-
temperature form would make wrong graduation answers a supported feature.

### Model default: `openai:gpt-5-mini`

Not a locked decision — `model_name` is admin-configurable and stores the
provider with the model (`anthropic:claude-opus-4-5` works unchanged), which is
what makes switching an `UPDATE` rather than a code change.

Defaulted to OpenAI because `OPENAI_API_KEY` is *already* mandatory for
embeddings (§3, locked), so one key runs the whole system. Defaulting to a
second provider would mean the agent silently cannot run for anyone who followed
the Phase 3 setup instructions. Seeded in migration `0005` rather than by
application code: the single-row CHECK makes concurrent bootstrap a race, and a
chat request that has to create its own configuration before answering is a
worse first experience than one that finds it there.

### Tools return prose, not JSON, and not the existing formatters

`academics.format_progress_table` exists and is deliberately not reused. It
renders fixed-width ASCII with abbreviated headers (`raw`, `crs`, `prog`) for
reading in a terminal during verification; a model reads better from explicit
labelled prose, and an abbreviation it has to guess at is an invitation to guess
wrong.

`search_documents` returns each passage with `[Source: …]` attached directly
above it rather than a JSON list with a parallel citations array — so the model
cannot pair the right quote with the wrong source.

`get_my_degree_progress` calls the shared `is_graduation_credit_complete` rather
than re-deriving "are we done?", so the tool and the endpoint cannot drift.

### Still open after Phase 4

- **Part 2 of the exit check has not been run.** The structural half passes.
  The behavioural half — two live sessions, the cross-student break attempt, the
  follow-up that tests memory — needs `OPENAI_API_KEY` and a running stack, the
  same blocker as Phase 3's exit check.
- **Phase 3's exit check is still unrun**, and the agent's grounding depends on
  it. `search_documents` returning the wrong section would now be wrong behind
  the agent, which is precisely what PROJECT_PLAN warned about.
- **No admin authentication.** `assistant_settings` has no write endpoint yet, so
  nothing is exposed; the admin login is Phase 6 work and must land with it.
- **`check_course_eligibility` and `request_advisor_appointment` are not tools
  yet** — Phase 5, as planned. The eligibility *endpoint* exists on `/me`.
- **Conversation history is trimmed by turn count, not tokens.** Forty messages
  is a guess that fits this corpus; a long thread would need a real budget.

---

## Phase 5 — eligibility and human-in-the-loop

### "Never auto-books" is two gates, not a prompt

CLAUDE.md §6 requires `request_advisor_appointment` to return a *proposal* and
require explicit confirmation before persisting. The obvious implementation —
two tools, plus a system-prompt instruction not to call the second until the
student agrees — is not good enough, for the same reason the Phase 4 scoping is
not a prompt. Models do call both in one turn when a user says "book me an
appointment," because that reads like consent. It isn't: the student has not yet
seen a time.

So confirmation is structural, via two independent gates:

1. **`propose` is a pure function.** It takes a session only to read the
   advisor's name, and there is no code path from it to a write.
   `verify_phase5.py` asserts this on the bytecode's symbol table — a canary
   rather than a mock, because the property being protected is "nobody adds a
   `session.add` here later."
2. **`confirm` requires a proposal in an *earlier turn*.** It reads the
   conversation's stored message history looking for a prior
   `request_advisor_appointment` tool call. Because the current turn's messages
   are persisted only after the run finishes, a proposal made moments ago in the
   same run is invisible — which is exactly what makes propose-and-confirm in
   one breath fail.

Gate 2 converts "the student must have had a chance to say no" into a database
query, which is why it can be tested without an LLM in the loop.

`ctx.deps.conversation_id` carries the thread, deliberately **not** a tool
argument — a model-supplied conversation id would let it point the gate at a
conversation where a proposal *did* happen. `verify_phase4.py` now asserts that
specifically.

### Slot times are generated, never proposed by the model

An appointment time is a fact about the world; a hallucinated one is the
invented-deadline failure §7 rule 1 forbids. `available_slots` is deterministic,
skips weekends and never proposes today. `confirm` rejects any time the generator
would not produce, so a model that invents one fails loudly.

The advising hours are an application default, not Handbook policy — §8 gives the
Advising Centre's remit and contact but no hours — and the proposal says so
rather than implying the Handbook specifies them.

### There is no `proposed` status, and no booking endpoint

`advisor_appointments` allows only `confirmed` and `cancelled`. A row means a
student said yes. Writing `status='proposed'` rows would have made gate 2 easier
to implement, but it contradicts the plan's contract that the tool "does not
write to the DB" — and it would make "the assistant booked something I did not
agree to" merely unlikely rather than impossible.

`GET /me/appointments` has no `POST` counterpart for the same reason: a booking
endpoint would be a second way in that bypasses the confirmation flow entirely.

`confirm` is idempotent on `(student_id, proposed_time)`, enforced by a unique
constraint. A model that calls it twice for one agreed slot should not produce
two appointments, and should not error either — the student's intent was
satisfied the first time.

### `check_course_eligibility` wraps Phase 2 rather than reimplementing it

The logic was built as a plain endpoint in Phase 2 precisely so this phase would
be a thin wrapper. The tool renders `reasons` as `[ok]` / `[BLOCKER]` lines
because the model's job is to *report* the finding, not to weigh it — and the
prompt tells it not to overrule a refusal because a transcript "looks fine",
since the load and probation caps are the part that is easy to miss.

An unknown course returns "there is no such course, check the code" rather than
"ineligible". Answering a question the student did not ask is a confident wrong
answer.

### Why MECH 310 is asked of all five students

The plan asks for the hybrid question against three; the test uses five, because
the three CENG students are the interesting negative cases. MECH 310 is not in
their programme and they have never taken MECH 210 — but "not your programme" is
not a refusal reason, "you have not passed the prerequisite" is, and a tool that
shrugged at an out-of-programme course would look correct while answering the
wrong question.

The pair that matters is Karim and Rania: same course, opposite answers, and
**for different reasons**. Rania is refused by the 9-credit probation cap, not by
the prerequisite — she has passed MECH 210. The test asserts the *reason*, not
just the boolean, because a refusal for the wrong reason would still score as a
pass on the plan's literal exit check.

### Still open after Phase 5

- **Parts 2 and 3 of the exit check have not been run.** The gates (part 1) pass
  with nothing required. The five-student eligibility matrix needs the database;
  the chat tests need a key. Same blocker as Phases 3 and 4.
- **Cancellation is modelled but unreachable.** `status='cancelled'` is a legal
  value with no tool or endpoint that sets it.
- **No advisor calendar.** Slots are generated from office hours, not from
  availability, so two students can be proposed the same time. Fine at five
  students; a real deployment needs the advisor's calendar.
- **The proposal is stateless between turns.** Gate 2 proves *a* proposal
  happened, not that the confirmed time is the one proposed — that is caught by
  the slot check instead, which is weaker. Storing proposals would close it, at
  the cost of the contract above.
