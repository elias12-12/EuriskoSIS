"""SQLAlchemy models for the curriculum and student record.

The source spreadsheet is deliberately flat -- no keys, types or constraints
declared -- so every constraint here is a modelling decision. The ones that
matter are commented; see DESIGN.md for the longer reasoning.

Covers the nine data sheets only. The app-owned tables from CLAUDE.md section 5
(`documents`, `document_chunks`, `assistant_settings`, `conversations`,
`messages`, `advisor_appointments`) arrive with the phases that need them --
`document_chunks.embedding` in particular cannot be declared until the embedding
model, and therefore its vector dimension, is chosen in Phase 3.
"""

from datetime import date, time
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


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
