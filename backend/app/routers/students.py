"""Student record endpoints -- Phase 2: plain HTTP, no agent.

**These are not the student-facing surface.** The student ID arrives as a path
parameter, which is right for the admin panel's browsers but is exactly what
CLAUDE.md section 7 rule 2 forbids for a student reading their own record. Phase 4
adds a `/me/*` surface that takes the ID from the authenticated session; because
every function in `records`/`academics`/`eligibility` already takes `student_id`
as an argument and filters on it in SQL, that is a routing change and nothing
deeper.

Until then nothing here may be called with an ID supplied by a student's browser.
"""

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.orm import Session

from app.academics import get_academic_summary, get_degree_progress
from app.config import get_settings
from app.db import get_session
from app.eligibility import check_eligibility
from app.records import (
    get_course_history,
    get_profile,
    get_schedule,
    student_exists,
)
from app.schemas import (
    CourseHistory,
    DegreeProgress,
    Eligibility,
    Profile,
    Schedule,
)

router = APIRouter(prefix="/students", tags=["students"])

StudentId = Path(
    description="University student ID, e.g. S2023011",
    examples=["S2023011"],
)


def _require_student(session: Session, student_id: str) -> None:
    """404 for an unknown ID.

    Kept separate from the record queries so 'no such student' never gets
    reported as an empty transcript -- a typo'd ID must not look like a student
    with no history, which is a real state (Lynn).
    """
    if not student_exists(session, student_id):
        raise HTTPException(status_code=404, detail=f"No student {student_id}")


@router.get("/{student_id}/profile", response_model=Profile)
def read_profile(
    student_id: str = StudentId,
    session: Session = Depends(get_session),
) -> Profile:
    profile = get_profile(session, student_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"No student {student_id}")
    return Profile(
        **profile, academics=get_academic_summary(session, student_id)
    )


@router.get("/{student_id}/schedule", response_model=Schedule)
def read_schedule(
    student_id: str = StudentId,
    session: Session = Depends(get_session),
) -> Schedule:
    """Current-term timetable. An empty list means registered for nothing."""
    _require_student(session, student_id)
    term = get_settings().current_term
    classes = get_schedule(session, student_id, term)
    return Schedule(
        student_id=student_id,
        term_code=term,
        total_credits=sum(c["credits"] for c in classes),
        classes=classes,
    )


@router.get("/{student_id}/courses", response_model=CourseHistory)
def read_courses(
    student_id: str = StudentId,
    session: Session = Depends(get_session),
) -> CourseHistory:
    """Full academic history, newest term first, including in-progress courses."""
    _require_student(session, student_id)
    return CourseHistory(
        student_id=student_id,
        academics=get_academic_summary(session, student_id),
        courses=get_course_history(session, student_id),
    )


@router.get("/{student_id}/degree-progress", response_model=DegreeProgress)
def read_degree_progress(
    student_id: str = StudentId,
    session: Session = Depends(get_session),
) -> DegreeProgress:
    """Per-category progress against the programme's requirements.

    `all_categories_satisfied` is the graduation credit test, not
    `credits_earned >= total_credits_required`: surplus credits in one category
    never offset a shortfall in another, so the totals can look healthy while a
    category is untouched.
    """
    profile = get_profile(session, student_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"No student {student_id}")

    categories = get_degree_progress(session, student_id)
    summary = get_academic_summary(session, student_id)
    return DegreeProgress(
        student_id=student_id,
        program_code=profile["program_code"],
        program_name=profile["program_name"],
        total_credits_required=profile["total_credits_required"],
        credits_earned=summary["credits_earned"],
        credits_in_progress=summary["credits_in_progress"],
        categories=categories,
        all_categories_satisfied=bool(categories)
        and all(c["is_satisfied"] for c in categories),
        unsatisfied_categories=[
            c["category_name"] for c in categories if not c["is_satisfied"]
        ],
    )


@router.get("/{student_id}/eligibility/{course_code:path}", response_model=Eligibility)
def read_eligibility(
    student_id: str = StudentId,
    course_code: str = Path(
        description=(
            "Course code. Contains a space, so URL-encode it as e.g. MECH%20310."
        ),
        examples=["MECH 310"],
    ),
    session: Session = Depends(get_session),
) -> Eligibility:
    """Whether the student may register for a course in the current term, and why.

    Combines prerequisites, the C- prerequisite gate, attempt limits, and
    load/probation caps. Two students can get opposite answers for the same
    course -- that is the point.
    """
    _require_student(session, student_id)
    result = check_eligibility(session, student_id, course_code)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No course {course_code}")
    return Eligibility(**result)
