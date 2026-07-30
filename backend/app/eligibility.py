"""Can this student register for this course in the current term?

Built as a plain endpoint before it is an agent tool (PROJECT_PLAN Phase 2), and
deliberately returns *why*, not just yes/no: a bare refusal is useless to a
student and unciteable by the assistant.

Every rule below is Handbook policy, with its threshold in `config.Settings` next
to the sentence it comes from. The rules are applied in Python rather than folded
into one SQL predicate because each needs to explain itself in prose -- the SQL
gathers facts, the Python judges them.

Blocking rules (all must pass):
  * the course must be offered in the current term;
  * every prerequisite must have been passed at C- or above;
  * the attempt limit (3) must not already be reached;
  * the student must not already be registered for it this term;
  * the resulting course load must be within the cap that applies to them.

Conditional, not blocking: a load above the standard maximum is permitted with
advisor approval when GPA allows, so it is reported as a condition rather than a
refusal.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.academics import get_academic_summary
from app.config import get_settings

COURSE_SQL = text(
    """
    SELECT
        c.course_code,
        c.title,
        c.credits,
        EXISTS (
            SELECT 1 FROM class_schedule cs
            WHERE cs.course_code = c.course_code AND cs.term_code = :term_code
        ) AS offered_this_term
    FROM courses c
    WHERE c.course_code = :course_code
    """
)

# Attempt history for the target course. `completed_attempts` counts every
# finished attempt including W and F, because the Handbook's three-attempt limit
# counts attempts, not passes.
ATTEMPTS_SQL = text(
    """
    WITH mine AS (
        SELECT e.status, e.grade, g.grade_points, g.earns_credit
        FROM enrollments e
        LEFT JOIN grading_scale g ON g.grade = e.grade
        WHERE e.student_id = :student_id AND e.course_code = :course_code
    )
    SELECT
        (SELECT COUNT(*) FROM mine WHERE status = 'Completed') AS completed_attempts,
        (SELECT COUNT(*) FROM mine WHERE status = 'In progress') AS in_progress_attempts,
        (SELECT MAX(grade_points) FROM mine WHERE status = 'Completed') AS best_grade_points,
        (SELECT bool_or(COALESCE(earns_credit, false)) FROM mine WHERE status = 'Completed')
            AS has_credit
    """
)

PREREQUISITES_SQL = text(
    """
    WITH best AS (
        SELECT
            e.course_code,
            g.grade,
            g.grade_points,
            ROW_NUMBER() OVER (
                PARTITION BY e.course_code
                ORDER BY g.grade_points DESC NULLS LAST, e.term_code DESC
            ) AS attempt_rank
        FROM enrollments e
        JOIN grading_scale g ON g.grade = e.grade
        WHERE e.student_id = :student_id AND e.status = 'Completed'
    )
    SELECT
        pr.prerequisite_course_code AS course_code,
        c.title,
        b.grade AS best_grade,
        b.grade_points AS best_grade_points,
        (b.grade_points IS NOT NULL AND b.grade_points >= :min_points) AS satisfied,
        EXISTS (
            SELECT 1 FROM enrollments e2
            WHERE e2.student_id = :student_id
              AND e2.course_code = pr.prerequisite_course_code
              AND e2.status = 'In progress'
        ) AS currently_taking
    FROM course_prerequisites pr
    JOIN courses c ON c.course_code = pr.prerequisite_course_code
    LEFT JOIN best b
      ON b.course_code = pr.prerequisite_course_code AND b.attempt_rank = 1
    WHERE pr.course_code = :course_code
    ORDER BY pr.prerequisite_course_code
    """
)

LOAD_SQL = text(
    """
    SELECT
        s.academic_status,
        COALESCE((
            SELECT SUM(e.credits) FROM enrollments e
            WHERE e.student_id = s.student_id
              AND e.term_code = :term_code
              AND e.status = 'In progress'
        ), 0) AS registered_credits
    FROM students s
    WHERE s.student_id = :student_id
    """
)


def _reason(rule: str, satisfied: bool, detail: str) -> dict[str, Any]:
    return {"rule": rule, "satisfied": satisfied, "detail": detail}


def check_eligibility(
    session: Session, student_id: str, course_code: str
) -> dict[str, Any] | None:
    """Evaluate registration eligibility. Returns None if the course is unknown.

    An unknown course is None rather than an ineligible result so the caller can
    answer 404 -- "you are not eligible for a course that does not exist" would be
    a confidently wrong answer.
    """
    settings = get_settings()
    term = settings.current_term
    params = {"student_id": student_id, "course_code": course_code}

    course = (
        session.execute(COURSE_SQL, {"course_code": course_code, "term_code": term})
        .mappings()
        .first()
    )
    if course is None:
        return None

    attempts = session.execute(ATTEMPTS_SQL, params).mappings().one()
    prerequisites = [
        dict(r)
        for r in session.execute(
            PREREQUISITES_SQL,
            params | {"min_points": settings.prerequisite_min_grade_points},
        ).mappings()
    ]
    load = (
        session.execute(LOAD_SQL, {"student_id": student_id, "term_code": term})
        .mappings()
        .one()
    )
    summary = get_academic_summary(session, student_id)

    reasons: list[dict[str, Any]] = []

    # --- already registered --------------------------------------------------
    already_registered = attempts["in_progress_attempts"] > 0
    if already_registered:
        reasons.append(
            _reason(
                "not_already_registered",
                False,
                f"You are already registered for {course_code} in {term}.",
            )
        )

    # --- offered this term ---------------------------------------------------
    # A course missing from the timetable is not offered, which is a real answer
    # rather than missing data (dataset README, note 4).
    if not course["offered_this_term"]:
        reasons.append(
            _reason(
                "offered_this_term",
                False,
                f"{course_code} is not offered in {term}.",
            )
        )

    # --- attempt limit -------------------------------------------------------
    attempts_used = attempts["completed_attempts"] + attempts["in_progress_attempts"]
    if attempts["completed_attempts"] >= settings.max_course_attempts:
        reasons.append(
            _reason(
                "attempt_limit",
                False,
                f"You have already attempted {course_code} "
                f"{attempts['completed_attempts']} times; no course may be "
                f"attempted more than {settings.max_course_attempts} times.",
            )
        )

    # --- prerequisites -------------------------------------------------------
    unmet = [p for p in prerequisites if not p["satisfied"]]
    for prereq in unmet:
        if prereq["currently_taking"]:
            detail = (
                f"{prereq['course_code']} is a prerequisite and you are taking it "
                f"now -- it must be completed at C- or above first."
            )
        elif prereq["best_grade"] is None:
            detail = (
                f"{prereq['course_code']} ({prereq['title']}) is a prerequisite and "
                f"you have not completed it."
            )
        else:
            detail = (
                f"{prereq['course_code']} is a prerequisite and requires C- or above; "
                f"your best grade is {prereq['best_grade']}."
            )
        reasons.append(_reason("prerequisite", False, detail))

    # --- course load ---------------------------------------------------------
    # Probation caps registration at 9 credits; otherwise the standard maximum is
    # 15, exceedable with advisor approval and a GPA of 3.00+.
    on_probation = load["academic_status"] == "Academic probation"
    registered = int(load["registered_credits"])
    # If they already hold a seat, its credits are inside `registered` already, so
    # adding them again would overstate the prospective load -- and registering for
    # a course you are registered for changes nothing anyway.
    added_credits = 0 if already_registered else int(course["credits"])
    prospective = registered + added_credits
    gpa: Decimal | None = summary["gpa"]
    requires_advisor_approval = False

    cap = (
        settings.probation_max_credits if on_probation else settings.standard_max_credits
    )
    # Skipped when already registered: the load is unchanged, so a load finding
    # would be a second, spurious reason on top of the real one.
    if added_credits and prospective > cap:
        if on_probation:
            reasons.append(
                _reason(
                    "course_load",
                    False,
                    f"You are on academic probation, which caps registration at "
                    f"{cap} credits. You are registered for {registered} and this "
                    f"course would take you to {prospective}.",
                )
            )
        elif gpa is not None and gpa >= settings.overload_min_gpa:
            requires_advisor_approval = True
            reasons.append(
                _reason(
                    "course_load",
                    True,
                    f"This would take you to {prospective} credits, above the "
                    f"standard maximum of {cap}. Permitted with advisor approval "
                    f"since your GPA is {gpa}.",
                )
            )
        else:
            have = "you have no GPA yet" if gpa is None else f"yours is {gpa}"
            reasons.append(
                _reason(
                    "course_load",
                    False,
                    f"This would take you to {prospective} credits, above the "
                    f"standard maximum of {cap}. Exceeding it needs advisor "
                    f"approval and a GPA of {settings.overload_min_gpa} or above; "
                    f"{have}.",
                )
            )

    eligible = all(r["satisfied"] for r in reasons)

    # A pass may be repeated once to improve the grade, so holding credit is
    # informational rather than disqualifying -- but the student should be told.
    notes: list[str] = []
    if attempts["has_credit"]:
        notes.append(
            f"You have already earned credit for {course_code}. A pass may be "
            f"repeated once to improve the grade; only the higher grade counts "
            f"toward your GPA and the credit is earned once."
        )
    if eligible and not prerequisites:
        notes.append(f"{course_code} has no prerequisites.")

    return {
        "student_id": student_id,
        "course_code": course["course_code"],
        "title": course["title"],
        "credits": course["credits"],
        "term_code": term,
        "eligible": eligible,
        "requires_advisor_approval": requires_advisor_approval,
        "offered_this_term": course["offered_this_term"],
        "prerequisites": prerequisites,
        "attempts_used": attempts_used,
        "already_registered": already_registered,
        "has_credit": bool(attempts["has_credit"]),
        "academic_status": load["academic_status"],
        "registered_credits": registered,
        "prospective_credits": prospective,
        "credit_cap": cap,
        "reasons": reasons,
        "notes": notes,
    }
