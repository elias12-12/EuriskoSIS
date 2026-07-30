"""Student record reads: profile, current-term schedule, course history.

Row-fetching lives here; computed values (GPA, degree progress) live in
`academics.py`, and registration rules in `eligibility.py`.

Every function takes `student_id` as an explicit argument and filters on it in
SQL. That is the scoping guarantee from CLAUDE.md section 7 rule 2 -- it holds
regardless of where the caller got the ID, so the Phase 4 switch from a path
parameter to the authenticated session identity is a routing change and nothing
more.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

PROFILE_SQL = text(
    """
    SELECT
        s.student_id,
        s.first_name,
        s.last_name,
        s.email,
        s.academic_status,
        s.advisor_name,
        s.scenario_note,
        s.program_code,
        p.program_name,
        p.total_credits_required,
        s.entry_term,
        et.term_name AS entry_term_name,
        s.expected_graduation_term
    FROM students s
    JOIN programs p ON p.program_code = s.program_code
    JOIN terms et ON et.term_code = s.entry_term
    WHERE s.student_id = :student_id
    """
)

# The schedule is the intersection of "registered this term" and "this term's
# timetable" -- an enrollment with no schedule row would be a data error, so the
# join is inner rather than left: silently showing a class with no time is worse
# than a visibly missing row.
SCHEDULE_SQL = text(
    """
    SELECT
        cs.course_code,
        c.title,
        e.credits,
        cs.days,
        cs.start_time,
        cs.end_time,
        cs.room,
        cs.instructor
    FROM enrollments e
    JOIN class_schedule cs
      ON cs.course_code = e.course_code AND cs.term_code = e.term_code
    JOIN courses c ON c.course_code = e.course_code
    WHERE e.student_id = :student_id
      AND e.term_code = :term_code
      AND e.status = 'In progress'
    ORDER BY cs.start_time, cs.course_code
    """
)

# Full history, newest term first. Ordered by the term's real start_date rather
# than term_code, because term codes sort alphabetically (FA before SP before SU)
# and would interleave academic years nonsensically.
COURSE_HISTORY_SQL = text(
    """
    SELECT
        e.term_code,
        t.term_name,
        t.start_date,
        e.course_code,
        c.title,
        e.credits,
        e.grade,
        g.grade_points,
        g.earns_credit,
        g.included_in_gpa,
        e.status
    FROM enrollments e
    JOIN terms t ON t.term_code = e.term_code
    JOIN courses c ON c.course_code = e.course_code
    LEFT JOIN grading_scale g ON g.grade = e.grade
    WHERE e.student_id = :student_id
    ORDER BY t.start_date DESC, e.course_code
    """
)


def student_exists(session: Session, student_id: str) -> bool:
    """Used to tell 'no such student' apart from 'student with no records'.

    Every other query in this module returns an empty result for both cases, and
    conflating them would let a typo'd ID look like an empty transcript.
    """
    return (
        session.execute(
            text("SELECT 1 FROM students WHERE student_id = :student_id"),
            {"student_id": student_id},
        ).scalar_one_or_none()
        is not None
    )


def get_profile(session: Session, student_id: str) -> dict[str, Any] | None:
    row = session.execute(PROFILE_SQL, {"student_id": student_id}).mappings().first()
    return dict(row) if row else None


def get_schedule(
    session: Session, student_id: str, term_code: str
) -> list[dict[str, Any]]:
    rows = session.execute(
        SCHEDULE_SQL, {"student_id": student_id, "term_code": term_code}
    ).mappings()
    return [dict(r) for r in rows]


def get_course_history(session: Session, student_id: str) -> list[dict[str, Any]]:
    rows = session.execute(COURSE_HISTORY_SQL, {"student_id": student_id}).mappings()
    return [dict(r) for r in rows]
