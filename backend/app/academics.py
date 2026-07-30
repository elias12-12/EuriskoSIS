"""Academic record calculations: GPA, credits earned, degree progress.

These are the queries every later layer depends on, so the policy they encode is
spelled out here rather than left implicit:

* **GPA** = sum(grade_points x credits) / sum(credits) over graded attempts only.
  W and P are excluded from both numerator and denominator, which falls out of
  joining `grading_scale.included_in_gpa` rather than being special-cased. F is
  included, at 0 points.
* **Repeats.** Only the highest completed attempt at a course counts toward GPA,
  and credit is earned once however many times a course is attempted (Handbook,
  repeating courses). Both fall out of ranking attempts per course and keeping the
  best one.
* **Category capping.** Credits counted toward a category are capped at that
  category's requirement, so surplus in one category can never mask a shortfall in
  another. This is the single most important rule in the brief and it is enforced
  by a LEAST() in SQL, not by hoping the caller remembers.
* **Nothing branches on a programme or category name.** The ALL vs ANY_N rule and
  the Major-Core C- gate are columns on `program_requirement_categories`
  (`selection_rule` / `courses_required` / `min_grade_points`), so one query serves
  both programmes and every category.

Every query is scoped by `student_id` in the SQL itself. Scoping is a data-layer
guarantee, not something a caller can forget (CLAUDE.md section 7, rule 2).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

# Ranking completed attempts per course, best grade first. Shared by the GPA and
# progress queries so the two can never disagree about which attempt counts.
#
# NULLS LAST matters: P carries no grade points but does earn credit, so a real
# graded attempt must outrank it rather than losing to a NULL. term_code breaks
# exact ties deterministically -- without it, two attempts at the same grade make
# the result depend on scan order.
_BEST_ATTEMPT_CTE = """
    best_attempt AS (
        SELECT
            e.course_code,
            e.credits,
            e.term_code,
            g.grade,
            g.grade_points,
            g.earns_credit,
            g.included_in_gpa,
            ROW_NUMBER() OVER (
                PARTITION BY e.course_code
                ORDER BY g.grade_points DESC NULLS LAST, e.term_code DESC
            ) AS attempt_rank
        FROM enrollments e
        JOIN grading_scale g ON g.grade = e.grade
        WHERE e.student_id = :student_id
          AND e.status = 'Completed'
    )
"""

GPA_SQL = text(
    f"""
    WITH {_BEST_ATTEMPT_CTE},
    for_gpa AS (
        SELECT credits, grade_points
        FROM best_attempt
        WHERE attempt_rank = 1 AND included_in_gpa
    ),
    for_credit AS (
        SELECT credits
        FROM best_attempt
        WHERE attempt_rank = 1 AND earns_credit
    )
    SELECT
        COALESCE((SELECT SUM(grade_points * credits) FROM for_gpa), 0) AS quality_points,
        COALESCE((SELECT SUM(credits) FROM for_gpa), 0)                AS gpa_credits,
        -- NULL, not 0.0, when nothing is graded yet: a first-term student has no
        -- GPA, and reporting 0.00 would read as failing (this is Lynn's case).
        (
            SELECT ROUND(SUM(grade_points * credits) / SUM(credits), 2)
            FROM for_gpa
            HAVING SUM(credits) > 0
        ) AS gpa,
        COALESCE((SELECT SUM(credits) FROM for_credit), 0) AS credits_earned,
        COALESCE((
            SELECT SUM(credits) FROM enrollments
            WHERE student_id = :student_id AND status = 'In progress'
        ), 0) AS credits_in_progress
    """
)

DEGREE_PROGRESS_SQL = text(
    f"""
    WITH me AS (
        SELECT student_id, program_code
        FROM students
        WHERE student_id = :student_id
    ),
    {_BEST_ATTEMPT_CTE},
    counted AS (
        -- One row per course whose credit the student actually holds.
        SELECT course_code, credits, grade_points
        FROM best_attempt
        WHERE attempt_rank = 1 AND earns_credit
    ),
    in_progress AS (
        SELECT e.course_code, e.credits
        FROM enrollments e
        WHERE e.student_id = :student_id AND e.status = 'In progress'
    )
    SELECT
        cat.category_id,
        cat.category_name,
        cat.credits_required,
        cat.selection_rule,
        cat.courses_required,
        cat.min_grade_points,
        offered.courses_offered,
        done.courses_counted,
        done.credits_counted,
        -- The cap. Surplus credits stay visible in credits_counted but never
        -- inflate credits_applied, so they cannot offset another category.
        LEAST(done.credits_counted, cat.credits_required) AS credits_applied,
        GREATEST(
            cat.credits_required - LEAST(done.credits_counted, cat.credits_required),
            0
        ) AS credits_remaining,
        -- Works for ALL and ANY_N alike: an ALL category's courses sum to exactly
        -- credits_required, so reaching the credit total is only possible with
        -- every course. No branch on selection_rule needed.
        (done.credits_counted >= cat.credits_required) AS is_satisfied,
        prog.courses_in_progress,
        prog.credits_in_progress
    FROM program_requirement_categories cat
    JOIN me ON me.program_code = cat.program_code
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS courses_offered
        FROM category_courses cc
        WHERE cc.category_id = cat.category_id
    ) offered ON TRUE
    LEFT JOIN LATERAL (
        SELECT
            COUNT(*) AS courses_counted,
            COALESCE(SUM(c.credits), 0) AS credits_counted
        FROM category_courses cc
        JOIN counted c ON c.course_code = cc.course_code
        WHERE cc.category_id = cat.category_id
          -- The Major Core C- gate, as data. A P (no grade points) also fails
          -- this, deliberately: a pass does not evidence C- or above.
          AND (cat.min_grade_points IS NULL OR c.grade_points >= cat.min_grade_points)
    ) done ON TRUE
    LEFT JOIN LATERAL (
        SELECT
            COUNT(*) AS courses_in_progress,
            COALESCE(SUM(p.credits), 0) AS credits_in_progress
        FROM category_courses cc
        JOIN in_progress p ON p.course_code = cc.course_code
        WHERE cc.category_id = cat.category_id
    ) prog ON TRUE
    ORDER BY cat.category_id
    """
)


def get_academic_summary(session: Session, student_id: str) -> dict[str, Any]:
    """GPA, credits earned and credits in progress for one student."""
    row = session.execute(GPA_SQL, {"student_id": student_id}).mappings().one()
    return dict(row)


def get_degree_progress(session: Session, student_id: str) -> list[dict[str, Any]]:
    """Per-category degree progress for one student, in category_id order.

    Returns an empty list for an unknown student_id -- the `me` CTE yields no rows,
    so there is nothing to join categories against. Callers distinguish "no such
    student" from "no progress" by checking the student exists first.
    """
    rows = session.execute(DEGREE_PROGRESS_SQL, {"student_id": student_id}).mappings()
    return [dict(r) for r in rows]


def is_graduation_credit_complete(progress: list[dict[str, Any]]) -> bool:
    """True only if EVERY category is individually satisfied.

    Exists as its own function because the tempting shortcut -- comparing total
    credits earned against the programme's 72 -- gives the wrong answer for a
    student with plenty of credits concentrated in the wrong categories. That is
    precisely the trap Jad (S2023027) is in the dataset to catch.
    """
    return bool(progress) and all(row["is_satisfied"] for row in progress)


def format_progress_table(progress: list[dict[str, Any]]) -> str:
    """Human-readable rendering, used by the verification script and by hand."""
    if not progress:
        return "  (no categories -- unknown student?)"

    lines = [
        f"  {'category':<36} {'rule':<9} {'applied':>9} {'req':>4} "
        f"{'raw':>4} {'crs':>7} {'prog':>5}  ok"
    ]
    for r in progress:
        rule = (
            "ALL"
            if r["selection_rule"] == "ALL"
            else f"ANY {r['courses_required']}"
        )
        lines.append(
            f"  {r['category_name']:<36} {rule:<9} "
            f"{r['credits_applied']:>9} {r['credits_required']:>4} "
            f"{r['credits_counted']:>4} "
            f"{str(r['courses_counted']) + '/' + str(r['courses_offered']):>7} "
            f"{r['credits_in_progress']:>5}  {'Y' if r['is_satisfied'] else 'n'}"
        )
    return "\n".join(lines)


def format_summary(summary: dict[str, Any]) -> str:
    gpa: Decimal | None = summary["gpa"]
    return (
        f"  GPA {gpa if gpa is not None else 'n/a (no graded courses)'}"
        f"   quality_points {summary['quality_points']}"
        f"   gpa_credits {summary['gpa_credits']}"
        f"   credits_earned {summary['credits_earned']}"
        f"   in_progress {summary['credits_in_progress']}"
    )
