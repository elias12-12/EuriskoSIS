"""SQLAlchemy models for the curriculum and student record.

The source spreadsheet is deliberately flat -- no keys, types or constraints
declared -- so every constraint here is a modelling decision. The ones that
matter are commented; see DESIGN.md for the longer reasoning.

Covers the nine data sheets, the two app-owned retrieval tables added in Phase 3
(`documents`, `document_chunks`), and the agent-layer tables added in Phase 4
(`student_sessions`, `assistant_settings`, `conversations`, `messages`).
`advisor_appointments` arrives with Phase 5, which is the phase that needs it.
"""

from datetime import date, datetime, time
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Fixed by the embedding model chosen in CLAUDE.md section 3: OpenAI
# text-embedding-3-small at its default 1536 dimensions. A schema fact, so it
# lives here rather than in config -- changing it is a migration, not a setting,
# and a value an admin could edit would silently invalidate every stored vector.
EMBEDDING_DIMENSIONS = 1536


# Values the Handbook defines for academic standing. 'Academic dismissal' does not
# appear in the current dataset but is a legal state per Handbook section 2, so the
# constraint allows it rather than forcing a migration the first time it occurs.
ACADEMIC_STATUSES = ("Good standing", "Academic probation", "Academic dismissal")

ENROLLMENT_STATUSES = ("Completed", "In progress")

# 'ALL'   -> every course in the category is required.
# 'ANY_N' -> any `courses_required` of the category's courses satisfy it.
SELECTION_RULES = ("ALL", "ANY_N")


class Term(Base):
    __tablename__ = "terms"

    term_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    term_name: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    __table_args__ = (
        CheckConstraint("end_date > start_date", name="ck_terms_dates_ordered"),
    )


class Program(Base):
    __tablename__ = "programs"

    program_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    program_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    total_credits_required: Mapped[int] = mapped_column(Integer, nullable=False)

    categories: Mapped[list["ProgramRequirementCategory"]] = relationship(
        back_populates="program"
    )

    __table_args__ = (
        CheckConstraint(
            "total_credits_required > 0", name="ck_programs_total_credits_positive"
        ),
    )


class ProgramRequirementCategory(Base):
    """One of the five requirement categories of a programme.

    `selection_rule` / `courses_required` exist so degree-progress logic can be
    written once against this table instead of special-casing a category by name.
    Neither value is in the spreadsheet -- the Catalogue states the rules in prose
    -- so the loader derives them and asserts the derivation; see
    `backend/scripts/load_spreadsheet.py`.
    """

    __tablename__ = "program_requirement_categories"

    category_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    program_code: Mapped[str] = mapped_column(
        ForeignKey("programs.program_code", ondelete="CASCADE"), nullable=False
    )
    category_name: Mapped[str] = mapped_column(String(64), nullable=False)
    credits_required: Mapped[int] = mapped_column(Integer, nullable=False)
    selection_rule: Mapped[str] = mapped_column(String(8), nullable=False)
    # Only meaningful for ANY_N; NULL for ALL, enforced by the CHECK below.
    courses_required: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Minimum grade points for a course to count toward THIS category, or NULL if
    # any credit-earning grade counts. Exists because the Handbook requires C-
    # (1.70) or above for Major Core specifically, while a D counts elsewhere.
    # A column rather than a branch: `if category_name == 'Major Core'` is exactly
    # what CLAUDE.md section 5's non-negotiable modelling rule forbids.
    min_grade_points: Mapped[Decimal | None] = mapped_column(
        Numeric(3, 2), nullable=True
    )

    program: Mapped["Program"] = relationship(back_populates="categories")
    category_courses: Mapped[list["CategoryCourse"]] = relationship(
        back_populates="category"
    )

    __table_args__ = (
        # A category name is unique within a programme but repeats across them
        # ('Engineering Core' exists for both), so the uniqueness is composite.
        UniqueConstraint("program_code", "category_name", name="uq_category_per_program"),
        CheckConstraint(
            f"selection_rule IN {SELECTION_RULES}", name="ck_category_selection_rule"
        ),
        # Keeps the two columns from drifting into a meaningless combination, e.g.
        # an ALL category that also claims "any 3".
        CheckConstraint(
            "(selection_rule = 'ALL' AND courses_required IS NULL) OR "
            "(selection_rule = 'ANY_N' AND courses_required > 0)",
            name="ck_category_courses_required_matches_rule",
        ),
        CheckConstraint("credits_required > 0", name="ck_category_credits_positive"),
    )


class Course(Base):
    __tablename__ = "courses"

    course_code: Mapped[str] = mapped_column(String(12), primary_key=True)
    title: Mapped[str] = mapped_column(String(96), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    prerequisites: Mapped[list["CoursePrerequisite"]] = relationship(
        back_populates="course",
        foreign_keys="CoursePrerequisite.course_code",
    )

    __table_args__ = (
        CheckConstraint("credits > 0", name="ck_courses_credits_positive"),
    )


class CategoryCourse(Base):
    """Which courses may satisfy which requirement category.

    A join table rather than a column on `courses`, because the same course
    satisfies different categories in different programmes -- MATH 101 is
    Engineering Core in both, but the category rows themselves are per-programme.
    """

    __tablename__ = "category_courses"

    category_id: Mapped[str] = mapped_column(
        ForeignKey("program_requirement_categories.category_id", ondelete="CASCADE"),
        primary_key=True,
    )
    course_code: Mapped[str] = mapped_column(
        ForeignKey("courses.course_code", ondelete="CASCADE"), primary_key=True
    )

    category: Mapped["ProgramRequirementCategory"] = relationship(
        back_populates="category_courses"
    )
    course: Mapped["Course"] = relationship()


class CoursePrerequisite(Base):
    """One row per prerequisite. Multiple rows for a course mean AND, never OR.

    The dataset contains no OR case and the Handbook enforces "all listed
    prerequisites", so there is deliberately no grouping/alternatives column: an
    unused OR mechanism would be a standing invitation to model it wrongly later.
    """

    __tablename__ = "course_prerequisites"

    course_code: Mapped[str] = mapped_column(
        ForeignKey("courses.course_code", ondelete="CASCADE"), primary_key=True
    )
    prerequisite_course_code: Mapped[str] = mapped_column(
        ForeignKey("courses.course_code", ondelete="CASCADE"), primary_key=True
    )

    course: Mapped["Course"] = relationship(
        back_populates="prerequisites", foreign_keys=[course_code]
    )
    prerequisite: Mapped["Course"] = relationship(
        foreign_keys=[prerequisite_course_code]
    )

    __table_args__ = (
        CheckConstraint(
            "course_code <> prerequisite_course_code", name="ck_prereq_not_self"
        ),
    )


class GradingScale(Base):
    """Grade to grade-point mapping, and how each grade is treated.

    A real table, joined against, rather than a Python dict: the W/P handling and
    the C- gate are institutional policy, and policy belongs in data where the
    admin panel can show it and a query can use it.

    `grade_points` is NULL for W and P (no points, excluded from GPA) and Numeric
    rather than float -- GPA is a ratio of sums over these values, and exact
    decimal arithmetic avoids a 1.7 that is really 1.6999999 deciding whether a
    prerequisite was met at the C- threshold.
    """

    __tablename__ = "grading_scale"

    grade: Mapped[str] = mapped_column(String(2), primary_key=True)
    grade_points: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    earns_credit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    included_in_gpa: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        # A grade counted in the GPA must have points to contribute; one excluded
        # from it must not pretend to have them.
        CheckConstraint(
            "(included_in_gpa AND grade_points IS NOT NULL) OR "
            "(NOT included_in_gpa AND grade_points IS NULL)",
            name="ck_grading_points_match_gpa_inclusion",
        ),
        CheckConstraint(
            "grade_points IS NULL OR (grade_points >= 0 AND grade_points <= 4)",
            name="ck_grading_points_range",
        ),
    )


class Student(Base):
    __tablename__ = "students"

    student_id: Mapped[str] = mapped_column(String(12), primary_key=True)
    first_name: Mapped[str] = mapped_column(String(64), nullable=False)
    last_name: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    program_code: Mapped[str] = mapped_column(
        ForeignKey("programs.program_code"), nullable=False
    )
    entry_term: Mapped[str] = mapped_column(
        ForeignKey("terms.term_code"), nullable=False
    )
    # Deliberately NOT a foreign key to terms, unlike entry_term. The values are
    # future terms (SP2027, SP2028, SP2030) and the Terms sheet only runs to
    # FA2026, so an FK would fail to load. Inventing the missing term rows would
    # mean fabricating start/end dates, which the assistant must never surface as
    # fact -- so this stays a plain code.
    expected_graduation_term: Mapped[str] = mapped_column(String(8), nullable=False)
    academic_status: Mapped[str] = mapped_column(String(24), nullable=False)
    advisor_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Dataset annotation describing the scenario each student exercises. Not
    # university data; kept because it is genuinely useful when verifying logic.
    scenario_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    program: Mapped["Program"] = relationship()
    enrollments: Mapped[list["Enrollment"]] = relationship(back_populates="student")

    __table_args__ = (
        CheckConstraint(
            f"academic_status IN {ACADEMIC_STATUSES}", name="ck_students_academic_status"
        ),
    )


class Enrollment(Base):
    """One attempt at one course in one term.

    The composite primary key is what makes repeats representable: a student may
    attempt the same course in different terms (Rania is repeating MATH 102 and
    PHYS 101), but not twice within one term -- verified against the source data
    before declaring it.

    `grade` is NULL exactly while `status` is 'In progress'. That is the whole
    reason GPA and credit queries must filter on the grade join rather than
    counting enrollment rows.
    """

    __tablename__ = "enrollments"

    student_id: Mapped[str] = mapped_column(
        ForeignKey("students.student_id", ondelete="CASCADE"), primary_key=True
    )
    term_code: Mapped[str] = mapped_column(
        ForeignKey("terms.term_code"), primary_key=True
    )
    course_code: Mapped[str] = mapped_column(
        ForeignKey("courses.course_code"), primary_key=True
    )
    # Credits as attempted. Currently always equal to courses.credits, but kept
    # because a transcript records what was awarded at the time, and a later
    # catalogue revision must not silently rewrite history.
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    grade: Mapped[str | None] = mapped_column(
        ForeignKey("grading_scale.grade"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    student: Mapped["Student"] = relationship(back_populates="enrollments")
    course: Mapped["Course"] = relationship()
    grading: Mapped["GradingScale | None"] = relationship()

    __table_args__ = (
        CheckConstraint(
            f"status IN {ENROLLMENT_STATUSES}", name="ck_enrollments_status"
        ),
        CheckConstraint(
            "(status = 'In progress' AND grade IS NULL) OR "
            "(status = 'Completed' AND grade IS NOT NULL)",
            name="ck_enrollments_grade_matches_status",
        ),
        CheckConstraint("credits > 0", name="ck_enrollments_credits_positive"),
    )


class ClassSchedule(Base):
    """When and where a course meets in a given term.

    Keyed by (term_code, course_code): the dataset offers exactly one section per
    course, verified before declaring it. Real multi-section scheduling would need
    a section identifier in the key, which is a migration, not a rewrite.

    A course absent from this table is simply not offered that term -- which is a
    real answer the eligibility tool has to give, not a missing row.
    """

    __tablename__ = "class_schedule"

    term_code: Mapped[str] = mapped_column(
        ForeignKey("terms.term_code"), primary_key=True
    )
    course_code: Mapped[str] = mapped_column(
        ForeignKey("courses.course_code"), primary_key=True
    )
    # Free text ('Mon, Wed', 'Tue, Thu', 'Fri') exactly as supplied. Parsing it
    # into a day set is presentation work and would lose the source formatting;
    # nothing in the brief needs to query by weekday.
    days: Mapped[str] = mapped_column(String(32), nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    room: Mapped[str] = mapped_column(String(24), nullable=False)
    instructor: Mapped[str] = mapped_column(String(64), nullable=False)

    course: Mapped["Course"] = relationship()

    __table_args__ = (
        CheckConstraint("end_time > start_time", name="ck_schedule_times_ordered"),
    )


# --- app-owned: document retrieval (Phase 3) --------------------------------

# 'pending'   -> row exists, nothing parsed yet
# 'ingesting' -> parse/embed in flight; chunks are incomplete and must not be searched
# 'ready'     -> chunks embedded and searchable
# 'failed'    -> see documents.error; chunks were rolled back
DOCUMENT_STATUSES = ("pending", "ingesting", "ready", "failed")

# What a chunk *is*, which is not the same as which document it came from. The
# two documents get different chunkers (CLAUDE.md section 4 / PROJECT_PLAN Phase
# 3), and this records the resulting shape so retrieval can be explained -- and,
# later, filtered -- without re-parsing.
CHUNK_KINDS = (
    "course",        # one Catalogue course entry, description + prerequisite line
    "program",       # one programme's requirement-category table
    "policy",        # one numbered Handbook section or subsection
    "overview",      # narrative front matter in either document
)


class Document(Base):
    """A source PDF the assistant is allowed to know things from.

    Keyed for re-ingestion by `filename`, not by id: the admin panel's "re-run
    ingestion" button re-uploads the same file, and the pipeline must replace
    that document's chunks rather than accumulate a second copy of them.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # Human title taken from the document itself, for citations: "Student
    # Handbook 2026-2027" reads better in an answer than the filename.
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Hash of the file bytes. Lets a re-ingest report "unchanged" honestly, and
    # ties a set of chunks to the exact file version that produced them -- which
    # matters because a citation to section 2.3 is only trustworthy if the text
    # behind it is the text that was embedded.
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Why the last ingestion failed, surfaced by the admin panel. A failed
    # ingestion that looks identical to an empty one is the thing to avoid.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint(f"status IN {DOCUMENT_STATUSES}", name="ck_documents_status"),
        CheckConstraint(
            "page_count IS NULL OR page_count > 0", name="ck_documents_page_count"
        ),
    )


class DocumentChunk(Base):
    """One retrievable passage, with everything needed to cite it.

    `section_ref` / `section_title` / `page` are not decoration: CLAUDE.md
    section 7 rule 5 requires every document-based answer to name its document
    and section, so a chunk that cannot say where it came from is unusable
    regardless of how well it matches.

    No vector index. At roughly seventy chunks an exact scan is both faster and
    *exact*; an IVFFlat index on this little data would trade correctness for a
    speedup that does not exist. Same reasoning as the "no indexes yet" note in
    Phase 1 -- revisit when there is a real query plan to improve.
    """

    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    # Position in the source document. Makes ingestion deterministic to compare
    # across runs, and lets a chunk's neighbours be fetched if an answer ever
    # needs surrounding context.
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # The embedded text, including its heading line -- see the chunkers in
    # `ingestion/`. The heading is part of the content on purpose: "Prerequisite:
    # MECH 210" is meaningless as a vector without "MECH 310 Fluid Mechanics"
    # attached to it.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=True
    )
    chunk_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Handbook: '1.3', '2.1', '9'. Catalogue: a course code or programme code.
    section_ref: Mapped[str | None] = mapped_column(String(32), nullable=True)
    section_title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # First page the chunk appears on. Sections 6 and 8 of the Handbook run
    # across a page boundary, so this is deliberately the start page rather than
    # a single page the whole chunk sits on -- see `ingestion/extract.py`.
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)

    document: Mapped["Document"] = relationship(back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_position"),
        CheckConstraint(f"chunk_kind IN {CHUNK_KINDS}", name="ck_chunk_kind"),
        CheckConstraint("page IS NULL OR page > 0", name="ck_chunk_page_positive"),
        CheckConstraint("length(content) > 0", name="ck_chunk_content_not_empty"),
        # The pipeline embeds in bulk after chunking, so a chunk is briefly
        # embedding-less; nothing enforces the eventual NOT NULL at the column
        # level. This index is what retrieval filters on, and it keeps the
        # delete-and-reinsert per document cheap.
        Index("ix_chunks_document", "document_id"),
    )


# --- app-owned: the agent layer (Phase 4) -----------------------------------


class StudentSession(Base):
    """An authenticated student session.

    Not in CLAUDE.md section 5's schema, and added deliberately. The brief says a
    student ID is sufficient identity and no password infrastructure is required
    -- but section 7 rule 2 also says every data access is scoped by the
    *authenticated* ID, enforced in the data layer and never supplied by the
    client or the model. Something has to hold that identity between the login
    call and the request, and a table is the honest place for it: no new
    dependency, revocable, and visible.

    The token is stored as a SHA-256 hash, never in the clear. Login is only a
    student ID, so a stolen token is no worse than a guessed ID -- but a database
    dump should not hand over live sessions, and hashing costs one line.
    """

    __tablename__ = "student_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    student_id: Mapped[str] = mapped_column(
        ForeignKey("students.student_id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Not security-critical; it is what lets the admin panel show who is active
    # and what makes an abandoned session obvious.
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    student: Mapped["Student"] = relationship()

    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="ck_session_expiry_after_start"),
        Index("ix_sessions_student", "student_id"),
    )


# The admin-configurable vocabularies. Constrained rather than free text because
# each value maps to a specific instruction fragment in the system prompt: an
# unrecognised tone would silently produce no instruction at all, which looks
# like the setting having no effect.
ASSISTANT_TONES = ("friendly", "neutral", "formal")
RESPONSE_LENGTHS = ("brief", "standard", "detailed")


class AssistantSettings(Base):
    """The admin panel's behaviour configuration. Exactly one row.

    Read at the start of *every* chat request, not once at startup: PROJECT_PLAN
    Phase 6 requires that changing a setting changes the very next response with
    no restart, and that is only true if nothing caches this.

    Deliberately holds no academic policy. The Handbook rules (the C- gate, the
    attempt limit, the load caps) live in `config.Settings` -- putting them in an
    admin-editable form would make wrong graduation answers a supported feature.
    """

    __tablename__ = "assistant_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tone: Mapped[str] = mapped_column(String(16), nullable=False)
    # Provider-qualified, as PydanticAI expects: 'openai:gpt-5-mini',
    # 'anthropic:claude-opus-4-5'. Storing the provider with the model is what
    # makes switching one a settings edit rather than a code change.
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    response_length: Mapped[str] = mapped_column(String(16), nullable=False)
    temperature: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # One row, enforced by the schema rather than by convention. "Which
        # settings row is live?" is not a question this app should ever have.
        CheckConstraint("id = 1", name="ck_settings_single_row"),
        CheckConstraint(f"tone IN {ASSISTANT_TONES}", name="ck_settings_tone"),
        CheckConstraint(
            f"response_length IN {RESPONSE_LENGTHS}", name="ck_settings_length"
        ),
        CheckConstraint(
            "temperature >= 0 AND temperature <= 2", name="ck_settings_temperature"
        ),
    )


class Conversation(Base):
    """One chat thread.

    `student_id` is nullable per CLAUDE.md section 5 -- an admin trying the
    assistant from the panel has no student identity -- but a conversation that
    *has* one is bound to it for life, and `conversations.py` refuses to load a
    thread for a different student. That check is what stops session memory from
    becoming a way around the scoping rules: the transcript of another student's
    conversation is that student's data.
    """

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[str | None] = mapped_column(
        ForeignKey("students.student_id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.id",
    )

    __table_args__ = (Index("ix_conversations_student", "student_id"),)


MESSAGE_ROLES = ("user", "assistant")


class Message(Base):
    """One turn in a conversation, stored twice on purpose.

    `role` and `content` are the human-readable record: what the Phase 6 chat
    panel renders, and what a person auditing the assistant reads.

    `model_message` is the PydanticAI `ModelMessage` serialised to JSON, and it
    is what gets replayed as history on the next turn. It is kept because the
    display projection is lossy -- it drops tool calls and their results -- and
    replaying only the prose would leave the model unable to see what it already
    looked up. "What about next term?" resolving correctly depends on it.

    Authoritative for replay: `model_message`. Authoritative for display:
    `role`/`content`. They are written in the same transaction, from the same
    run, so they cannot disagree about what happened.
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_message: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")

    __table_args__ = (
        CheckConstraint(f"role IN {MESSAGE_ROLES}", name="ck_messages_role"),
        Index("ix_messages_conversation", "conversation_id"),
    )
