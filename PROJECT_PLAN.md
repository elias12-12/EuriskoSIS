# Eurisko University Assistant — Project Plan

This is the working roadmap. `CLAUDE.md` (same folder) is the file to drop into your
repo root so Claude Code has the architecture decisions and data model on hand while
you work through this phase by phase. Update the "Current phase" line in `CLAUDE.md`
as you move through this plan.

---

## Phase 0 — Repo & environment (target: half a day)

- [x] Create the shared repo. Root layout from day one:
  ```
  /backend        (FastAPI + PydanticAI, managed with uv)
  /frontend       (React)
  /ingestion      (parsing/chunking/embedding scripts, can be called by backend)
  docker-compose.yml
  CLAUDE.md
  README.md
  DESIGN.md
  ```
- [x] `docker-compose.yml` with two services minimum: `db` (postgres + pgvector image,
  e.g. `pgvector/pgvector:pg16`) and `backend`. Add `frontend` once it exists.
- [x] Get `docker compose up` running an empty FastAPI app that can `SELECT 1` against
  Postgres, before writing any real feature. This is the single most common place
  teams lose a day later — do it now while it's cheap.
- [x] `uv init` in `backend/`, add `fastapi`, `pydantic-ai`, `sqlalchemy`, `psycopg[binary]`,
  `alembic` (or a plain SQL migration approach — your call), `pandas`, `openpyxl`.
  Chose Alembic. Also added `uvicorn[standard]` (server), `pgvector` (VECTOR type for
  SQLAlchemy/Alembic) and `pydantic-settings` (env-based config).

**Exit check:** `docker compose up` from a clean clone gives you a running API that can
talk to the database. Nothing else needs to work yet.

---

## Phase 1 — Data modeling and loading (this is the phase that separates projects)

- [x] Design the schema (full version in `CLAUDE.md` → Data Model). Key decisions to
  lock in before writing SQL:
  - `category_courses` is a join table, not a column on `courses` — a course can
    satisfy different categories in different programmes.
  - Track "any N of M" vs "all required" per category (a `courses_required` /
    `nullable "any of"` flag on `program_requirement_categories`, or a separate
    small table) so degree-progress logic never special-cases a category by name.
  - `course_prerequisites` is one row per prerequisite — multiple rows for one
    course means AND, never OR. Don't add an OR case; the data doesn't have one.
  - Store `grading_scale` as a real table (grade → points, earns_credit,
    included_in_gpa) and join against it — never hardcode a Python dict.
- [x] Write a one-off loader script: pandas reads each sheet, inserts into the
  matching table, in dependency order (terms → programs → categories → courses →
  category_courses → prerequisites → students → enrollments → schedule → grading_scale).
  `backend/scripts/load_spreadsheet.py`; re-runnable (wipes and reinserts), and it
  derives `selection_rule` rather than hand-transcribing it.
- [x] **Hand-verify Maya's GPA** (`S2023011`) against `Enrollments` + `Grading_Scale`
  before moving on: sum(grade_points × credits) / sum(credits), excluding any
  W/P rows. If your query doesn't match your by-hand math, stop here.
  231.40 / 64 = **3.62**, matches by hand and in pandas.
- [x] **Hand-verify degree progress for Jad** (`S2023027`) — he's the trap case
  (lots of credits, but weak on Gen Ed and hasn't started capstone). If your
  query says he's close to graduating by credit count alone, the query is wrong.
  55 credits = CORE 28 / ELEC 9 / GEN 3 / MAJ 15 / PROF 0; 3 of 5 categories
  unmet including the whole capstone track. GPA 153.00 / 55 = **2.78**.

**Exit check:** you can write one SQL query that computes degree progress per
category for any student ID, and it gives the right answer for all 5 students
without an `if program ==` anywhere.

---

## Phase 2 — Plain API endpoints, no agent

- [x] `GET /students/{id}/profile`
- [x] `GET /students/{id}/schedule` (current term = FA2026, from `Class_Schedule_FA2026`)
- [x] `GET /students/{id}/courses` (full academic history)
- [x] `GET /students/{id}/degree-progress`
- [x] `GET /students/{id}/eligibility/{course_code}` — build this as a plain
  endpoint before it's a tool. It needs: course's prerequisites, whether each was
  passed at C- or above, and (if relevant) load/probation constraints from the
  Handbook rules you've encoded.
  Also covers attempt limits, whether the course is offered this term, and
  already-registered. Returns *why*, not just yes/no.
- [x] Test **every endpoint against all 5 students**, not just Maya. Rania
  (probation, repeats) and Lynn (zero history) are the ones that break naive
  implementations.
  `backend/scripts/verify_phase2.py` — 5 endpoints x 5 students plus a 7-course
  eligibility matrix and the 404 paths. Rania is capped at 9 credits by probation
  so every additional course is refused; Lynn has no GPA, so she cannot satisfy
  the 3.00 needed to exceed 15 credits.

**Exit check:** Swagger UI, hit every endpoint for every student ID, answers
match what you'd compute by hand from the spreadsheet.

---

## Phase 3 — Document ingestion (RAG), tested standalone

- [x] Parse the two PDFs with different strategies — the Catalogue is short
  structured entries and tables; the Handbook is dense prose with numbered
  sections. One generic chunker will hurt one of the two.
  `ingestion/extract.py` (pypdf, page-tagged line stream — Handbook sections 6
  and 8 cross a page boundary mid-table, so chunking page by page would split
  them), `ingestion/catalogue.py`, `ingestion/handbook.py`, routed by filename
  in `ingestion/parse.py`. Neither chunker has a chunk size.
- [x] Catalogue: chunk per course description (with its prerequisite line kept
  attached — never split a course from its prereqs), and separately per
  programme/category table (page 1-2 content).
  39 chunks: 33 courses, 2 programmes, 4 overview. The chunker asserts both the
  33 count and that every course chunk still holds a prerequisite line.
- [x] Handbook: chunk per numbered subsection (1.1, 1.2, 2.2, 3, 5, 6, 9, etc.),
  keeping tables (grading scale, calendar, fee table, routing table) intact
  as single chunks — splitting the routing table (section 9) mid-row makes it
  useless for "who do I contact" questions.
  19 chunks: 18 sections + front matter, asserted against the full expected
  section list. Section 5 deliberately keeps both term calendars together.
- [x] Embed and store in `pgvector` with metadata: source file, section/course
  reference, page number. You need this metadata for citations later.
  `documents` / `document_chunks` in Alembic revision `0004`, `VECTOR(1536)`.
  `RetrievedChunk.citation()` renders the source string an answer must quote.
- [x] Build the ingestion pipeline as re-runnable (delete-and-reinsert per
  document, keyed by filename) so the admin panel's "re-run ingestion" button
  has something real to call.
  `ingestion/pipeline.py`; skips unchanged files by sha256 (`--force` after a
  chunker change), and a failed run leaves no chunks but does leave the reason.
- [x] Hand-verify every chunk boundary by eye — `scripts/inspect_chunks.py`,
  which needs neither the database nor an API key. This is the one Phase 3
  decision no later test can catch: a wrong boundary does not raise, it
  produces a passage that retrieves well and answers badly. 58 chunks read.
- [ ] **Test retrieval directly, no agent involved**, with a fixed test set:
  - "What are the prerequisites for CENG 320?"
  - "How many credits do I need in General Education?"
  - "When is the last day to drop a course without a W?"
  - "What happens if I fail a required course?"
  - "How is my GPA calculated?"
  - "Who do I contact about a scholarship?"
  If any of these retrieve the wrong section, fix retrieval before touching the agent.
  Written as `backend/scripts/verify_phase3.py` — asserts the expected
  `section_ref` per question, not just that something plausible came back
  (1.1 is the grading scale, 1.2 is the GPA formula; only one answers "how is my
  GPA calculated"). **Not yet run: needs `OPENAI_API_KEY`.**

**Exit check:** for each test question above, the top retrieved chunk actually
contains the answer, and you can point to which document/section it came from.
**Not met yet** — everything up to the embedding call is built and hand-verified,
but the check itself needs a key and a running database.

To close this phase:

```
docker compose up -d --build
docker compose exec backend python scripts/ingest_documents.py
docker compose exec backend python scripts/verify_phase3.py
```

---

## Phase 4 — Wire the agent (PydanticAI)

- [x] **Add the `/me/*` surface first** — carried forward from Phase 2 as a
  correctness issue, not a gap. `/students/{id}/*` takes the ID as a path
  parameter, which section 7 rule 2 forbids for a student reading their own
  record. `app/auth.py` (session tokens, hashed, in `student_sessions`) and
  `app/routers/me.py`, both delegating to the same Phase 2 query functions.
- [x] Start with exactly two tools: `search_documents` and `get_my_schedule`.
  The goal of this step isn't the tools — it's proving the authenticated
  student ID flows from the HTTP request, into the agent's dependency/context
  object, into the tool call, without the model ever being asked to supply or
  confirm the student ID itself.
  Enforced by shape: the scoped tools take **no parameters at all**, so the
  schema the model sees is `{"properties": {}, "additionalProperties": false}`.
  There is no field for an injection to fill. The ID travels in `StudentContext`
  (PydanticAI `deps`), which the model never sees.
- [x] Add `get_my_courses`, `get_my_degree_progress` once the pattern above works.
- [x] Add session memory: store conversation history (even just in Postgres,
  keyed by session/student) and pass enough of it back in so "what about next
  term?" resolves without the user repeating themselves.
  `conversations`/`messages` (revision `0005`). Each turn is stored twice: prose
  for the UI, the serialised PydanticAI message for replay — replaying only the
  prose would drop the tool calls the follow-up depends on. A conversation is
  student-scoped data and another student's thread is a 404.
- [x] Wire the admin behaviour config (tone, model, response length, temperature)
  as a DB-backed settings row, read at the start of each request and compiled
  into the system prompt / model call — not read once at startup.
  `assistant_settings` (one row, enforced by CHECK, seeded in the migration) and
  `app/assistant_config.py`. Explicitly not cached. The fixed behaviour rules are
  concatenated before the admin's fragments so settings cannot override them.

**Exit check:** ask the agent "what's my schedule" as two different students in
two different sessions and confirm each gets only their own data — then try to
break it by asking "what's S2023011's schedule" as a different student, and
confirm it refuses.

`backend/scripts/verify_phase4.py`, in two parts:

- **Part 1, structural — PASSING.** Asserts no scoped tool exposes a parameter
  capable of naming a student. Needs no API key, model or database
  (`--structural`). This is the real guarantee; a test that only checked the
  model's *behaviour* would pass on a well-behaved model even if the tools took
  a `student_id` argument.
- **Part 2, behavioural — NOT YET RUN.** Two live sessions, the cross-student
  break attempt, and a follow-up that tests session memory. Needs
  `OPENAI_API_KEY` and the running stack — the same blocker as Phase 3.

```
docker compose up -d --build
docker compose exec backend python scripts/verify_phase4.py --structural   # works now
docker compose exec backend python scripts/verify_phase4.py                # needs a key
```

---

## Phase 5 — Eligibility tool and human-in-the-loop

- [x] `check_course_eligibility(course_code)`: combine (a) prerequisite list from
  `course_prerequisites`, (b) whether each prerequisite was passed at C- or
  above, (c) Handbook load/probation constraints if relevant, (d) course-load
  cap for probation students. Test against Rania specifically — she's the
  probation case this tool exists for.
  A thin wrapper over the Phase 2 endpoint, which is why that was built first.
  Renders `reasons` as `[ok]`/`[BLOCKER]` lines; the model reports the finding
  rather than weighing it.
- [x] `request_advisor_appointment(...)`: the tool returns a **proposed**
  appointment object (time, advisor, reason) — it does not write to the DB.
  The agent presents the proposal in chat; only an explicit follow-up
  confirmation from the user triggers a second call that actually persists it.
  Enforced by two gates, not by the prompt: `propose` has no code path to a
  write, and `confirm` requires a proposal in an **earlier turn** — the current
  turn's messages are not persisted until the run ends, so propose-and-confirm
  in one breath fails. `advisor_appointments` (revision `0006`) has no
  `proposed` status and there is no booking endpoint.
- [x] Test the full hybrid question from the brief end to end: *"Am I allowed
  to register for MECH 310?"* for Karim and for Rania — should give different
  answers for the same course based on their individual transcripts.

**Exit check:** the hybrid eligibility question above works correctly for at
least 3 of the 5 students, including one who should be told "no."

`backend/scripts/verify_phase5.py`, in three parts by what each needs:

- **Part 1, the gates — PASSING, needs nothing** (`--gates`): no database, API,
  key or model. `propose` cannot write; slots are deterministic, never same-day,
  never at a weekend; an invented time is not a slot.
- **Part 2, the eligibility matrix — NOT YET RUN, needs the database**
  (`--structural`): all five students against MECH 310. Asserts Rania is refused
  **by the probation credit cap, not the prerequisite** — a refusal for the wrong
  reason would still pass the exit check as literally worded.
- **Part 3, through the agent — NOT YET RUN, needs a key**: the question asked as
  Karim and as Rania, then an attempt to make the agent book in a single turn.

```
docker compose exec backend python scripts/verify_phase5.py --gates         # works now
docker compose exec backend python scripts/verify_phase5.py --structural    # needs the db
docker compose exec backend python scripts/verify_phase5.py                 # needs a key
```

---

## Phase 6 — UIs

- [ ] Student portal: profile, schedule, history, GPA/credits, degree progress
  (per category, earned vs. required, visually distinct from "surplus credits
  in one category don't offset another" — this is the #1 thing students in
  the brief get wrong), chat panel.
- [ ] Admin panel: document upload/list/remove/re-ingest, student/course/enrollment
  browsers (filterable), behaviour config form (tone/model/length/temperature)
  that writes straight to the settings table.
- [ ] Confirm changing a behaviour setting changes the very next chat response,
  with no restart.

---

## Phase 7 — DESIGN.md

Write this **as you go**, not at the end — you'll lose the actual reasoning
behind chunking/caching decisions otherwise. Keep a running notes file from
Phase 1 onward and distill it into the final 1-2 pages last.

Must answer: database choice and why, chunking/retrieval strategy and why,
what you cached and why, what you'd do differently with two more weeks.

---

## Cross-cutting checks to run throughout (not a separate phase)

- [ ] Every personal-data tool/endpoint takes the authenticated student ID from
  the session/auth layer, never from a parameter the model or client supplies.
- [ ] Every document-grounded answer includes a source citation.
- [ ] Ask a question with no answer in the documents (e.g. something invented)
  and confirm the assistant says "I don't know" and points to the right office
  from Handbook section 9, rather than guessing.
- [ ] Re-run the full 5-student test matrix after any change to degree-progress
  or eligibility logic — it's the easiest place to silently regress.
