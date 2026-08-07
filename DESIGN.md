# Design

Eurisko University assistant — a student portal and admin panel over a FastAPI
backend, a PydanticAI agent, and a single PostgreSQL database.

Every team gets the same dataset, so the differentiator is architecture. This
document is the distillation; the phase-by-phase working record, including the
options that were rejected and what is still unfinished, is in
[DESIGN_NOTES.md](DESIGN_NOTES.md).

---

## 1. Database: one PostgreSQL instance with `pgvector`

The alternative was a dedicated vector store beside a relational database.
Rejected, because **every rule this application has to enforce is relational.**

A retrieved chunk is useless without the document row that lets it be cited. A
degree-progress answer is a join across five tables and a per-category rule. An
eligibility answer is a transcript joined to a prerequisite graph joined to a
grading scale. Two stores would mean two connection pools, no foreign keys across
the boundary, and application-level joins reassembling what one `JOIN` already
does.

What one database bought concretely:

- **Scoping is enforced by foreign keys**, not by convention. `enrollments`,
  `conversations`, `advisor_appointments` and `student_sessions` all cascade from
  `students`.
- **Policy lives in data, not in branches.** `program_requirement_categories`
  carries `selection_rule`, `courses_required` and `min_grade_points`, so degree
  progress is one query for both programmes — never `if program_code == 'BE-CENG'`.
  The C− Major Core gate is a nullable column, applied in SQL as
  `(min_grade_points IS NULL OR grade_points >= min_grade_points)`.
- **The category cap is a SQL expression**: `LEAST(credits_counted,
  credits_required)`. Surplus credits cannot leak into another category because
  the query cannot express it.

Cost accepted: at real corpus size, ANN indexing and relational workloads would
compete for the same instance. At 58 chunks that is theoretical.

## 2. Chunking and retrieval

**Two chunkers, because one generic chunker measurably hurts one document.** The
Catalogue is a directory — 33 rigidly formatted course entries and two
requirement tables. The Handbook is dense prose under a numbered hierarchy.

**Neither chunker has a chunk size.** Both cut on structure the documents already
declare, because that structure *is* a human statement of where one idea ends.

The invariant that drove the Catalogue design: **a course is never separated from
its prerequisite line.** A fixed-window chunker over page 3 would routinely put
one course's `Prerequisite:` line in the same chunk as the *next* course's title —
so "What are the prerequisites for CENG 320?" retrieves a passage containing the
string "CENG 320" and a prerequisite belonging to CENG 330. That is not a near
miss; it is a confident wrong answer, the outcome the brief calls worst. The
chunker asserts the count is 33 and that every course chunk still holds a
prerequisite line.

Following the Handbook's own numbering gave three things at once: one idea per
chunk, every table intact (grading scale, add/drop deadlines, calendar, fees,
awards, services, routing), and **citations for free** — `section_ref` is the
document's own "2.3", checkable by a human holding the PDF.

Three supporting decisions:

- **Extraction is a page-tagged line stream, not a list of pages.** Handbook
  sections 6 and 8 cross a page boundary mid-table; chunking page by page would
  split both, including the services directory.
- **The heading lives inside the chunk.** `Prerequisite: MECH 210` embeds to
  almost nothing alone; under `MECH 310 Fluid Mechanics` it answers a question.
  Course chunks also carry their subject heading.
- **One chunk is deliberately coarse.** Handbook section 5 keeps both term
  calendars together. Splitting them would sharpen retrieval, but "the last day
  to drop without a W" has a *different answer per term*, and a chunk holding one
  of them invites an answer that is precisely wrong about the other.

Retrieval began as pure cosine distance over 58 chunks, with a note that a
lexical channel was the obvious upgrade "if an exact-code query ranks a
neighbouring course first — but that is a hypothesis, and the six-question test
set is what decides it."

**The test set decided it: pure vector search failed three of six.** Every
failure was a passage containing the query's words verbatim that the embedding
could not separate from its neighbours — `CENG 320` returned CENG 420 and
CENG 330 ahead of it, and "who do I contact about a scholarship" returned the
section describing the awards over the routing table that lists the address.

So a Postgres full-text channel now runs alongside the vector one, fused by
**reciprocal rank fusion** (`1/(60 + rank)`, summed). RRF rather than a weighted
score blend because cosine similarity and `ts_rank_cd` are not on comparable
scales; normalising them would mean inventing a conversion and then tuning it,
whereas ranks need no calibration. The result is visible in the fixed test: for
CENG 320 the winning chunk has similarity **0.650** and the runner-up **0.692** —
the fusion promotes the lower-similarity chunk precisely because both channels
agree on it. All six now pass.

Still **no index on either channel**: at this size an exact scan beats an
approximate structure and, unlike IVFFlat, cannot miss the true nearest
neighbour, and `to_tsvector` over 58 short rows is not measurable.

The sequence matters more than the outcome. Building the hybrid up front would
have produced the same passing test set and taught us nothing about whether the
second channel earned its place.

## 3. What we cached, and what we deliberately did not

The line is: **cache what is immutable within a process, or keyed by content
hash. Never cache what an administrator can change, or what a correct answer
depends on.**

Cached:

- **Environment configuration** — `get_settings()` is `@lru_cache`d. It reads
  immutable process environment; parsing it per request would be waste.
- **Embeddings, keyed by content hash.** `documents.sha256` lets a re-ingestion
  of an unchanged file skip the API entirely. That is what allows the admin
  panel's "re-run ingestion" to be a button rather than a job queue. `--force`
  covers the case the hash misses: the PDF is identical but a chunker changed.
- **Dependency layers in both images** — manifests copied before source, so a
  code edit does not reinstall the world.

Deliberately not cached:

- **`assistant_config.load()` runs at the start of every chat turn.** Caching it
  would break the single behaviour the admin panel exists to demonstrate: a
  settings change must alter the *very next* response with no restart. The
  requirement is satisfied by where the read happens, not by the form that writes.
- **Retrieval results.** No query cache. The corpus is 58 chunks and the scan is
  exact; a cache surviving a re-ingestion would cite a section whose text had
  changed.
- **GPA, credits and degree progress.** Computed per request in SQL. A
  materialised transcript summary is the classic way for a correction to stop
  showing up.

## 4. Enforcement is structural, not prompted

The project's main architectural claim. Three examples:

- **No scoped agent tool has a parameter that could name a student.**
  `get_my_schedule` and its siblings take nothing; the JSON schema the model sees
  is `{"properties": {}, "additionalProperties": false}`. There is no field for a
  prompt injection to fill. The ID travels in the PydanticAI `deps` object, which
  the model never sees, sourced only from `auth.current_student` — the single
  producer of an authenticated ID in the application.
- **The assistant cannot book an appointment without a yes.** `propose` has no
  code path to a write; `confirm` requires a proposal in an *earlier* turn, and
  since the current turn's messages are persisted only after the run finishes, a
  propose-and-confirm in one breath fails.
- **Administrators and students are separate tables and separate dependencies**,
  so a token of one kind cannot be accepted where the other belongs.

In each case the system prompt *also* asks for the right behaviour. That is a
courtesy which produces a good refusal message; delete it and the guarantee
still holds. This is why the verification scripts test the schemas and the gates
directly, and not only the model's behaviour — a well-behaved model would hide a
broken control.

## 5. Verification status

**Every exit check now passes against a running stack**, model calls included,
via a Pydantic AI Gateway key that covers both the agent and the embeddings.

- All seven migrations apply cleanly, and `alembic revision --autogenerate`
  against the upgraded database produces an **empty** revision — zero drift
  between the models and the four hand-written migrations.
- Phase 1: GPA, credits and every requirement category recomputed from the
  spreadsheet in pandas and diffed against the SQL for all five students, plus a
  fabricated student exercising the two rules the real five cannot.
- Phase 2: five endpoints × five students, plus the 404 paths.
- Phase 3: 58 chunk boundaries read by eye, and **6 of 6** retrieval questions
  returning a top chunk that contains the answer.
- Phase 4: structural scoping across all seven tools, plus two live sessions
  seeing only their own data and a cross-student request refused with a citation
  to Handbook §4.1.
- Phase 5: the human-in-the-loop gates; MECH 310 across all five students
  asserting *which rule* blocks each; and, live, a proposal that books nothing
  followed by a confirmation in a later turn that does.
- Phase 6: both directions of the cross-principal refusal, the settings
  round-trip, the browsers' filters, and a `response_length` change moving the
  very next reply from 599 to 2,933 characters with no restart.

### What the first real run cost

Running it end to end for the first time found **two genuine bugs and three wrong
assertions of fact**, and the split is the useful part.

Bugs, both invisible until the stack ran:

- A Docker **anonymous volume was serving a virtualenv built before three
  dependencies existed** — the image was correct and the mount was stale. Fixed
  by moving the venv to `/opt/venv`, outside the bind mount, so there is nothing
  to shadow.
- `/me/chat` **had no missing-credentials guard**, so it raised a 500 where
  `/documents/search` had returned a clean 503 since Phase 3 — and the 500 lost
  its CORS headers, because Starlette's error middleware sits outside
  `CORSMiddleware`. The browser reported only `TypeError: Failed to fetch`. A
  catch-all exception handler now keeps error responses inside the middleware
  stack, so *every* future 500 is readable, not just this one.

Wrong facts, all of them mine, all asserted from memory rather than checked:
Karim was assumed eligible for MECH 310 when he is **already registered** for it;
Rania was written up as having passed MECH 210 when she has **never taken it**;
and one Phase 5 assertion passed for the wrong reason, matching `"eligible"`
inside `"not eligible"`.

**The logic verified by construction held. The facts asserted from memory did
not, and neither did the parts of the environment nobody had exercised.** That is
the argument for the structural tests in §4 and against trusting a documented
dataset over a query against it.

## 6. With two more weeks

1. **A golden-set evaluation for the agent**, not just for retrieval: fixed
   questions with expected citations and expected refusals, run on every prompt
   or model change. Retrieval has six regression questions; the agent's answers
   have none, so a settings change is still unmeasured beyond reply length.
2. **Widen the retrieval test set.** Six questions chose the retrieval strategy
   and three of them failed on the first run — that is a small sample to be
   steering on. The next fifty would tell us whether the RRF constant and the
   candidate width are right or merely not obviously wrong.
3. **Structured table extraction** (`pdfplumber`) for real cell structure instead
   of flattened reading order. The lexical channel has made this less pressing —
   the routing table is now found by exact phrase — but the grading scale still
   embeds as six interleaved columns.
4. **Derive academic standing rather than storing it.** It currently comes from
   the registrar as given; the two-consecutive-review-terms dismissal rule
   (Summer excluded) is unimplemented, and a divergence would be invisible.
5. **Finish the unmodelled Handbook rules**: the four-W limit over a degree, and
   the add/drop/withdraw deadlines, which are cited in answers but not enforced.
6. **Token-budgeted conversation history** instead of a 40-turn cap, and stored
   proposals so a confirmation can verify *which* proposal it answers.
7. **Per-administrator accounts and an audit log.** One shared password with no
   record of who changed a setting is adequate for a demonstration and not for
   anything else.
