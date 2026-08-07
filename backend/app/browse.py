"""Filterable list queries for the admin panel's browsers.

Read-only, and separate from `records.py` on purpose. Everything in `records`
takes a `student_id` and filters on it in SQL, because that is the scoping
guarantee; these functions deliberately return *many* students, so mixing them
into the same module would put a scoped and an unscoped query side by side and
invite one to be used where the other belongs.

Nothing here is reachable without `auth.current_admin`.

Paging is offset/limit with a total count. Not cursor-based: the dataset is five
students and eighty-four enrollments, the browsers sort by a stable key, and a
cursor would be machinery for a problem this data cannot have.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.models import Course, CoursePrerequisite, Enrollment, Student


@dataclass(frozen=True)
class Page:
    total: int
    items: list[dict[str, Any]]


def _paginate(session: Session, statement: Select, offset: int, limit: int) -> Page:
    """Run a statement twice: once for the count, once for the page.

    The count is taken from the *filtered* statement with ordering stripped, so
    the browser can show "12 of 84 matching" rather than "12 of everything",
    which is the number that tells an admin whether their filter worked.
    """
    total = session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery())
    )
    rows = session.execute(statement.offset(offset).limit(limit)).mappings().all()
    return Page(total=total or 0, items=[dict(row) for row in rows])


def students(
    session: Session,
    *,
    search: str | None = None,
    program_code: str | None = None,
    academic_status: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> Page:
    """Students, optionally filtered by free text, programme or standing."""
    statement = select(
        Student.student_id,
        Student.first_name,
        Student.last_name,
        Student.email,
        Student.program_code,
        Student.entry_term,
        Student.expected_graduation_term,
        Student.academic_status,
        Student.advisor_name,
    ).order_by(Student.student_id)

    if search:
        pattern = f"%{search.strip()}%"
        statement = statement.where(
            or_(
                Student.student_id.ilike(pattern),
                Student.first_name.ilike(pattern),
                Student.last_name.ilike(pattern),
                Student.email.ilike(pattern),
            )
        )
    if program_code:
        statement = statement.where(Student.program_code == program_code)
    if academic_status:
        statement = statement.where(Student.academic_status == academic_status)

    return _paginate(session, statement, offset, limit)


def courses(
    session: Session,
    *,
    search: str | None = None,
    subject: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> Page:
    """Courses with their prerequisites collapsed into one string per row.

    Aggregated in SQL rather than fetched per course, because a browser listing
    33 courses that issues 34 queries is the classic N+1 and there is no reason
    to write it that way even at this size.
    """
    prerequisites = (
        select(
            CoursePrerequisite.course_code,
            func.string_agg(
                CoursePrerequisite.prerequisite_course_code,
                ", ",
            ).label("prerequisites"),
        )
        .group_by(CoursePrerequisite.course_code)
        .subquery()
    )

    statement = (
        select(
            Course.course_code,
            Course.title,
            Course.credits,
            Course.description,
            func.coalesce(prerequisites.c.prerequisites, "").label("prerequisites"),
        )
        .outerjoin(prerequisites, prerequisites.c.course_code == Course.course_code)
        .order_by(Course.course_code)
    )

    if search:
        pattern = f"%{search.strip()}%"
        statement = statement.where(
            or_(Course.course_code.ilike(pattern), Course.title.ilike(pattern))
        )
    if subject:
        # Subject prefix, e.g. 'MECH'. Matched on the code rather than stored
        # separately: the prefix is derivable, and a denormalised column would be
        # a second source for the same fact.
        statement = statement.where(Course.course_code.ilike(f"{subject.strip()} %"))

    return _paginate(session, statement, offset, limit)


def enrollments(
    session: Session,
    *,
    student_id: str | None = None,
    course_code: str | None = None,
    term_code: str | None = None,
    grade: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> Page:
    """Enrollment rows, joined to names so the browser is readable.

    `student_id` here is a *filter*, not a scope. This is the admin surface;
    omitting it returns every student's rows, which is the whole point of a
    browser and is why this module is gated behind `current_admin`.
    """
    statement = (
        select(
            Enrollment.student_id,
            Student.first_name,
            Student.last_name,
            Enrollment.term_code,
            Enrollment.course_code,
            Course.title,
            Enrollment.credits,
            Enrollment.grade,
            Enrollment.status,
        )
        .join(Student, Student.student_id == Enrollment.student_id)
        .join(Course, Course.course_code == Enrollment.course_code)
        .order_by(
            Enrollment.student_id, Enrollment.term_code, Enrollment.course_code
        )
    )

    if student_id:
        statement = statement.where(Enrollment.student_id == student_id)
    if course_code:
        statement = statement.where(Enrollment.course_code == course_code)
    if term_code:
        statement = statement.where(Enrollment.term_code == term_code)
    if grade:
        statement = statement.where(Enrollment.grade == grade)

    return _paginate(session, statement, offset, limit)


def filter_options(session: Session) -> dict[str, list[str]]:
    """Distinct values for the browsers' dropdowns.

    Queried rather than hardcoded so the filters cannot drift from the data --
    a dropdown offering a programme that no longer exists is a small lie the UI
    tells confidently.
    """
    return {
        "programs": list(
            session.scalars(
                select(Student.program_code).distinct().order_by(Student.program_code)
            ).all()
        ),
        "academic_statuses": list(
            session.scalars(
                select(Student.academic_status)
                .distinct()
                .order_by(Student.academic_status)
            ).all()
        ),
        "terms": list(
            session.scalars(
                select(Enrollment.term_code).distinct().order_by(Enrollment.term_code)
            ).all()
        ),
        "grades": list(
            session.scalars(
                select(Enrollment.grade)
                .where(Enrollment.grade.is_not(None))
                .distinct()
                .order_by(Enrollment.grade)
            ).all()
        ),
        "subjects": sorted(
            {
                code.split(" ")[0]
                for code in session.scalars(select(Course.course_code)).all()
            }
        ),
    }
