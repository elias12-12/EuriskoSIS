# Eurisko University Assistant — Project Instructions

This file is project context for Claude Code. Read it before doing anything.
It contains locked-in architecture decisions, the full data model, the tool
spec, and how I want us to work. Don't re-litigate the decisions marked
LOCKED — if one seems wrong, say so and ask, don't just change it.

See `PROJECT_PLAN.md` in this same repo for the phase-by-phase build order.
**Current phase: 0 — repo & environment.** Update this line as we move through phases.

---

## 1. What we're building

A university assistant web app with two surfaces:

1. **Student portal** — a student logs in with just their student ID (no
   password infra needed), sees their own record (profile, schedule, academic
   history, GPA, degree progress), and chats with an assistant.
2. **Admin panel** — an administrator uploads/manages the source documents,
   browses the underlying data, and configures the assistant's behaviour
   (tone, model, response length, temperature) without touching code.

The assistant answers two fundamentally different kinds of questions and has
to know which is which:
- **Document questions** (policy, deadlines, fees, course descriptions) → retrieval
  over the two PDFs, answered only from retrieved content, always cited.
- **Personal questions** (my schedule, my grades, my degree progress) → structured
  queries scoped strictly to the logged-in student ID.
- **Hybrid questions** (e.g. "Am I allowed to register for MECH 310?") need both:
  prerequisites + minimum-grade rule from the Handbook + this student's transcript.

Every team gets identical data. The differentiator is architecture, not the dataset.

---

## 2. Fixed technology (do not change)

| Layer | Tool |
|---|---|
| Backend | FastAPI |
| Agent framework | PydanticAI |
| Frontend | React |
| Containerisation | Docker + Docker Compose |
| Package management | `uv` (backend) |
| Version control | Git, shared repo |

## 3. Our choices (LOCKED unless we explicitly revisit)

- **Database: single PostgreSQL instance**, using the `pgvector` extension for
  embeddings, rather than a separate vector DB or a second database engine.
  Rationale: one schema, one connection pool, real foreign keys enforcing the
  scoping rules, and no justification yet for a second store. If a specific
  query pattern later proves this wrong, we revisit — we don't add a second
  DB "because we've seen three."
- **Migrations**: [DECIDE: Alembic vs. plain SQL scripts — pick one in Phase 0
  and note the choice here.]
- **Embeddings**: [DECIDE: OpenAI `text-embedding-3-small` vs. a local
  sentence-transformers model — note the choice and why once made.]
- **Auth**: student ID is sufficient identity for the student portal; admin has
  a separate, simpler login. No password infrastructure required by the brief,
  but the authenticated identity must be carried through every layer (HTTP →
  agent context → every tool call) and used to scope every data access. This
  is enforced at the tool/data layer, never by asking the model nicely.

---

## 4. Data sources (the only three files the assistant may know anything from)

- `Eurisko_University_Course_Catalogue_2026-2027.pdf` (5 pages) — degree
  structure, 5 requirement categories, both programmes, all 33 course
  descriptions with prerequisites.
- `Eurisko_University_Student_Handbook_2026-2027.pdf` (6 pages) — grading/GPA,
  academic standing, repeating courses, load limits, prerequisite enforcement,
  add/drop/withdraw, graduation requirements, integrity/privacy, academic
  calendar, tuition/fees, financial aid, student services, and a routing table
  (section 9) naming the responsible office per enquiry type.
- `Eurisko_University_Data.xlsx` — 9 flat sheets, no keys/types/constraints
  declared (that's our job): `Terms`, `Program_Requirements`, `Courses`,
  `Category_Courses`, `Course_Prerequisites`, `Students`, `Enrollments`,
  `Class_Schedule_FA2026`, `Grading_Scale`.

### Key facts from the documents that the data model and logic must encode

- **5 requirement categories, two different satisfaction rules**:
  Engineering Core (28 cr, all 10 courses required), General Education
  (9 cr, any 3 of 4 courses), Major Core (18 cr, all 6 programme-specific
  courses required), Professional Practice & Capstone (8 cr, all 3 required),
  Technical Electives (9 cr, any 3 of the 4 ENGR 450-series). **Surplus credits
  in one category never offset a shortfall in another.**
- **Grading**: A=4.0 down to F=0.0, standard scale. W = withdrawn, no credit,
  excluded from GPA. P = pass, credit earned, excluded from GPA. GPA =
  sum(grade_points × credits) / sum(credits) over graded courses only
  (W and P excluded from both numerator and denominator).
- **C- or above is a distinct gate**, separate from "earns credit": a D still
  earns credit, but C- or above is required (a) to use a course as a
  prerequisite for another, and (b) for a course to count toward Major Core.
  A student below C- in such a course keeps the credit but must repeat.
- **Academic standing**: Good standing (GPA ≥ 2.00), Academic probation
  (GPA < 2.00) → registration capped at 9 credits, scholarship-ineligible,
  requires an agreed study plan with advisor. Academic dismissal (GPA < 2.00
  for two consecutive Fall/Spring review terms). Summer terms are not review points.
- **Repeats**: F must be repeated if required; a pass may be repeated once to
  improve grade; only the higher grade counts toward GPA; credit earned once
  only; no course may be attempted more than 3 times; repeat surcharge applies.
- **Load**: full-time = 9-15 credits in Fall/Spring; >15 needs advisor approval
  + GPA ≥ 3.00; probation caps at 9.
- **Registration/prerequisites**: all listed prerequisites required, at C- or
  above, enforced at registration; waiver only in writing by the offering department.
- **Add/drop/withdraw** (Fall 2026 dates): add by 18 Sep 2026, drop without W
  by 25 Sep 2026, withdraw with W by 13 Nov 2026 (max 4 W's over a degree).
- **Graduation**: 72 credits with every category individually satisfied, GPA
  ≥ 2.00, C- or above in every Major Core course, placement + capstone done,
  no outstanding financial balance, application submitted by the deadline
  (20 Nov 2026 for Spring 2027) — missing the deadline excludes that cycle
  regardless of record completeness.
- **Section 9 routing table** is the fallback: when a question needs a human,
  route to the correct office (Registrar, Academic Advising, Student Financial
  Services, Career Services, Health & Wellness, Accessibility, Conduct, IT,
  or Campus Security for anything safety-related).

### The five students (login identities) — deliberately not equivalent

| ID | Name | Programme | Situation |
|---|---|---|---|
| S2023011 | Maya Haddad | CENG | 4th year, on track, one term from graduating |
| S2023027 | Jad Mansour | CENG | 4th year, >half credits but weak Gen Ed, hasn't started capstone — **credit total alone gives the wrong graduation answer** |
| S2024019 | Karim Nassar | MECH | 3rd year, ordinary progression |
| S2025008 | Rania Khoury | MECH | 2nd year, **on academic probation**, repeating two failed courses |
| S2026042 | Lynn Abou Chakra | CENG | 1st term, no completed courses, no GPA yet |

Every degree-progress / eligibility change must be re-tested against all five,
especially Jad (the credit-count trap) and Rania (probation constraints) and
Lynn (empty-history edge cases).

---

## 5. Data model (target schema — adjust types/constraints as needed, keep the shape)

```
terms(term_code PK, term_name, start_date, end_date)
programs(program_code PK, program_name, total_credits_required)
program_requirement_categories(
  category_id PK, program_code FK -> programs,
  category_name, credits_required,
  selection_rule  -- 'ALL' (every course in category required) or 'ANY_N' with n
)
courses(course_code PK, title, credits, description)
category_courses(category_id FK, course_code FK)         -- many-to-many
course_prerequisites(course_code FK, prerequisite_course_code FK)  -- multi-row = AND
students(student_id PK, first_name, last_name, email, program_code FK,
         entry_term FK, expected_graduation_term FK, academic_status,
         advisor_name)
enrollments(student_id FK, term_code FK, course_code FK, credits, grade, status)
class_schedule(term_code, course_code FK, days, start_time, end_time, room, instructor)
grading_scale(grade PK, grade_points, earns_credit BOOL, included_in_gpa BOOL)

-- app-owned, not from the spreadsheet:
documents(id PK, filename, uploaded_at, status)
document_chunks(id PK, document_id FK, content, embedding VECTOR, section_ref, page)
assistant_settings(id PK, tone, model_name, response_length, temperature, updated_at)
conversations(id PK, student_id FK NULLABLE, created_at)
messages(id PK, conversation_id FK, role, content, created_at)
advisor_appointments(id PK, student_id FK, proposed_time, status, created_at, confirmed_at)
```

**Non-negotiable modelling rule**: degree-progress and eligibility logic must
be written generically against `category_courses` / `selection_rule` — never
`if program_code == 'BE-CENG'`. If you catch yourself writing that, the schema
needs a column, not the code a branch.

---

## 6. Required tools (agent layer)

| Tool | Returns | Scoped to student? |
|---|---|---|
| `search_documents(query)` | Relevant passages, with source + section for citation | No |
| `get_my_schedule()` | Current-term (FA2026) classes: days, times, rooms, instructors | **Yes** |
| `get_my_courses()` | Courses taken/in progress, with grades | **Yes** |
| `get_my_degree_progress()` | Credits earned vs. required, per category, per the category's selection rule | **Yes** |
| `check_course_eligibility(course_code)` | Eligible or not, and why — combining prerequisites + C-or-above rule + transcript + load/probation constraints | **Yes** |
| `request_advisor_appointment(...)` | A **proposed** appointment only — never auto-books; requires explicit user confirmation before persisting | **Yes** |

The student ID for every "Yes" tool comes from the authenticated session context
passed into the agent, never from a model-supplied argument.

---

## 7. Required behaviour (rules, not features — apply every time)

1. **Grounded answers only** for document questions. If it's not in the
   documents, say so and point to the right office from the Handbook's routing
   table. Never invent a deadline, fee, or policy.
2. **Strict data scoping**, enforced in the tool/data layer using the
   authenticated student ID. A student asking about another student's record
   is refused, not redirected or softened.
3. **Session memory** — follow-ups resolve from context without the user
   repeating the whole question.
4. **Honest uncertainty** — "I don't know" is a correct, acceptable answer.
   A confident wrong answer is the worst outcome.
5. **Cite sources** for every document-based answer (which document, which section).

---

## 8. How I want us to work

- Follow `PROJECT_PLAN.md` phase by phase. Don't jump ahead to the agent or
  UI before the plain data layer (Phase 1-2) is verified against all 5 students.
- Before writing code for a new phase, briefly restate the plan for that phase
  and check it against this file, especially the LOCKED decisions above.
- When a decision in this file says [DECIDE], stop and ask me rather than
  picking silently — update this file with the answer once we decide.
- Prefer small, reviewable steps: schema → loader → verify by hand → endpoint
  → verify by hand → tool → verify by hand. Don't build three layers at once.
- Any change to degree-progress or eligibility logic needs a quick re-check
  against all 5 students (Maya, Jad, Karim, Rania, Lynn) before moving on —
  Jad and Rania are the ones that catch bugs.
- Write a short docstring or comment explaining *why* on anything non-obvious
  (e.g. why a chunk boundary was chosen, why a query is structured a certain way)
  — this feeds directly into DESIGN.md later.
- Keep secrets/config in `.env`, never hardcoded, even for a student project.
