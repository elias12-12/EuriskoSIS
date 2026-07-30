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

- [ ] Design the schema (full version in `CLAUDE.md` → Data Model). Key decisions to
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
- [ ] Write a one-off loader script: pandas reads each sheet, inserts into the
  matching table, in dependency order (terms → programs → categories → courses →
  category_courses → prerequisites → students → enrollments → schedule → grading_scale).
- [ ] **Hand-verify Maya's GPA** (`S2023011`) against `Enrollments` + `Grading_Scale`
  before moving on: sum(grade_points × credits) / sum(credits), excluding any
  W/P rows. If your query doesn't match your by-hand math, stop here.
- [ ] **Hand-verify degree progress for Jad** (`S2023027`) — he's the trap case
  (lots of credits, but weak on Gen Ed and hasn't started capstone). If your
  query says he's close to graduating by credit count alone, the query is wrong.

**Exit check:** you can write one SQL query that computes degree progress per
category for any student ID, and it gives the right answer for all 5 students
without an `if program ==` anywhere.

---

## Phase 2 — Plain API endpoints, no agent

- [ ] `GET /students/{id}/profile`
- [ ] `GET /students/{id}/schedule` (current term = FA2026, from `Class_Schedule_FA2026`)
- [ ] `GET /students/{id}/courses` (full academic history)
- [ ] `GET /students/{id}/degree-progress`
- [ ] `GET /students/{id}/eligibility/{course_code}` — build this as a plain
  endpoint before it's a tool. It needs: course's prerequisites, whether each was
  passed at C- or above, and (if relevant) load/probation constraints from the
  Handbook rules you've encoded.
- [ ] Test **every endpoint against all 5 students**, not just Maya. Rania
  (probation, repeats) and Lynn (zero history) are the ones that break naive
  implementations.

**Exit check:** Swagger UI, hit every endpoint for every student ID, answers
match what you'd compute by hand from the spreadsheet.

---

## Phase 3 — Document ingestion (RAG), tested standalone

- [ ] Parse the two PDFs with different strategies — the Catalogue is short
  structured entries and tables; the Handbook is dense prose with numbered
  sections. One generic chunker will hurt one of the two.
- [ ] Catalogue: chunk per course description (with its prerequisite line kept
  attached — never split a course from its prereqs), and separately per
  programme/category table (page 1-2 content).
- [ ] Handbook: chunk per numbered subsection (1.1, 1.2, 2.2, 3, 5, 6, 9, etc.),
  keeping tables (grading scale, calendar, fee table, routing table) intact
  as single chunks — splitting the routing table (section 9) mid-row makes it
  useless for "who do I contact" questions.
- [ ] Embed and store in `pgvector` with metadata: source file, section/course
  reference, page number. You need this metadata for citations later.
- [ ] Build the ingestion pipeline as re-runnable (delete-and-reinsert per
  document, keyed by filename) so the admin panel's "re-run ingestion" button
  has something real to call.
- [ ] **Test retrieval directly, no agent involved**, with a fixed test set:
  - "What are the prerequisites for CENG 320?"
  - "How many credits do I need in General Education?"
  - "When is the last day to drop a course without a W?"
  - "What happens if I fail a required course?"
  - "How is my GPA calculated?"
  - "Who do I contact about a scholarship?"
  If any of these retrieve the wrong section, fix retrieval before touching the agent.

**Exit check:** for each test question above, the top retrieved chunk actually
contains the answer, and you can point to which document/section it came from.

---

## Phase 4 — Wire the agent (PydanticAI)

- [ ] Start with exactly two tools: `search_documents` and `get_my_schedule`.
  The goal of this step isn't the tools — it's proving the authenticated
  student ID flows from the HTTP request, into the agent's dependency/context
  object, into the tool call, without the model ever being asked to supply or
  confirm the student ID itself.
- [ ] Add `get_my_courses`, `get_my_degree_progress` once the pattern above works.
- [ ] Add session memory: store conversation history (even just in Postgres,
  keyed by session/student) and pass enough of it back in so "what about next
  term?" resolves without the user repeating themselves.
- [ ] Wire the admin behaviour config (tone, model, response length, temperature)
  as a DB-backed settings row, read at the start of each request and compiled
  into the system prompt / model call — not read once at startup.

**Exit check:** ask the agent "what's my schedule" as two different students in
two different sessions and confirm each gets only their own data — then try to
break it by asking "what's S2023011's schedule" as a different student, and
confirm it refuses.

---

## Phase 5 — Eligibility tool and human-in-the-loop

- [ ] `check_course_eligibility(course_code)`: combine (a) prerequisite list from
  `course_prerequisites`, (b) whether each prerequisite was passed at C- or
  above, (c) Handbook load/probation constraints if relevant, (d) course-load
  cap for probation students. Test against Rania specifically — she's the
  probation case this tool exists for.
- [ ] `request_advisor_appointment(...)`: the tool returns a **proposed**
  appointment object (time, advisor, reason) — it does not write to the DB.
  The agent presents the proposal in chat; only an explicit follow-up
  confirmation from the user triggers a second call that actually persists it.
- [ ] Test the full hybrid question from the brief end to end: *"Am I allowed
  to register for MECH 310?"* for Karim and for Rania — should give different
  answers for the same course based on their individual transcripts.

**Exit check:** the hybrid eligibility question above works correctly for at
least 3 of the 5 students, including one who should be told "no."

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
