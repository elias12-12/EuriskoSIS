"""Load Eurisko_University_Data.xlsx into Postgres.

Run inside the backend container:

    docker compose exec backend python scripts/load_spreadsheet.py

Re-runnable: it deletes every row it owns, in reverse dependency order, then
reinserts. That matters because this will be re-run every time the schema
changes during Phase 1, and a loader that only works against an empty database
is a loader you stop trusting.

Two things this script does beyond copying cells:

1. **Derives `selection_rule`.** The spreadsheet has `credits_required` per
   category but no indication of whether every course is required or only N of
   them -- that rule is prose in the Catalogue. It is recoverable from the data:
   if a category's courses carry exactly `credits_required` credits, all of them
   are needed; if they carry more, it is a choose-N category. The derivation
   looks at no category or programme names, which is what lets degree-progress
   logic stay generic (CLAUDE.md section 5, non-negotiable modelling rule).

2. **Asserts the derivation** against the rules the Catalogue states, keyed by the
   category_id suffix (CORE/GEN/MAJ/PROF/ELEC) so the check itself is
   programme-independent. If the two ever disagree the load fails loudly rather
   than quietly seeding a wrong rule that every later answer depends on.
"""

from __future__ import annotations

import sys
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import delete, func, insert, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    CategoryCourse,
    ClassSchedule,
    Course,
    CoursePrerequisite,
    Enrollment,
    GradingScale,
    Program,
    ProgramRequirementCategory,
    Student,
    Term,
)

WORKBOOK_NAME = "Eurisko_University_Data.xlsx"

# What the Catalogue states, by category_id suffix. Used only to verify the
# derivation below -- never to produce it.
EXPECTED_RULES: dict[str, tuple[str, int | None]] = {
    "CORE": ("ALL", None),      # Engineering Core: all 10 courses, 28 credits
    "GEN": ("ANY_N", 3),        # General Education: any 3 of 4
    "MAJ": ("ALL", None),       # Major Core: all 6 programme-specific courses
    "PROF": ("ALL", None),      # Professional Practice & Capstone: all 3
    "ELEC": ("ANY_N", 3),       # Technical Electives: any 3 of the 4 ENGR 450-series
}

# Handbook policy, not spreadsheet data and not derivable from it: a course only
# counts toward Major Core at C- (1.70) or above, whereas a D earns credit
# everywhere else. Keyed by category_id suffix so it stays programme-independent.
# This is the loader encoding a documented rule into the schema, which is the
# whole point of the min_grade_points column -- the alternative is a name check
# inside the degree-progress query.
MIN_GRADE_POINTS_BY_SUFFIX: dict[str, Decimal | None] = {
    "CORE": None,
    "GEN": None,
    "MAJ": Decimal("1.70"),
    "PROF": None,
    "ELEC": None,
}

# Reverse dependency order for the wipe; foreign keys make any other order fail.
DELETE_ORDER = [
    Enrollment,
    ClassSchedule,
    CategoryCourse,
    CoursePrerequisite,
    Student,
    ProgramRequirementCategory,
    Course,
    GradingScale,
    Program,
    Term,
]


def to_time(value: object) -> dt_time:
    """Coerce a schedule time to `datetime.time`.

    pandas reads 'HH:MM' cells as strings in this workbook, but whether it does so
    depends on how Excel stored them -- so accept an already-parsed time too
    rather than depending on inference that could change with a pandas release.
    """
    if isinstance(value, dt_time):
        return value
    return pd.to_datetime(str(value), format="%H:%M").time()


def yes_no_to_bool(series: pd.Series) -> pd.Series:
    """The sheet spells booleans 'Yes'/'No'; anything else is a data error."""
    unknown = set(series.dropna().unique()) - {"Yes", "No"}
    if unknown:
        raise ValueError(f"expected only Yes/No, found {unknown}")
    return series == "Yes"


def derive_selection_rules(
    reqs: pd.DataFrame, category_courses: pd.DataFrame, courses: pd.DataFrame
) -> dict[str, tuple[str, int | None]]:
    """Work out ALL vs ANY_N per category from credits alone.

    No category or programme name is consulted. A category whose courses carry
    exactly the required credits needs all of them; one that offers more credits
    than required is a choose-N, where N is credits_required divided by the
    per-course credit value. ANY_N is only well defined when the choosable courses
    all carry the same credits -- true for both such categories here (4 x 3cr) and
    checked rather than assumed.
    """
    with_credits = category_courses.merge(
        courses[["course_code", "credits"]], on="course_code", how="left"
    )
    if with_credits.credits.isna().any():
        missing = with_credits[with_credits.credits.isna()].course_code.tolist()
        raise ValueError(f"Category_Courses references unknown courses: {missing}")

    rules: dict[str, tuple[str, int | None]] = {}
    for row in reqs.itertuples():
        group = with_credits[with_credits.category_id == row.category_id]
        if group.empty:
            raise ValueError(f"category {row.category_id} has no courses")

        offered = int(group.credits.sum())
        required = int(row.credits_required)

        if offered == required:
            rules[row.category_id] = ("ALL", None)
            continue
        if offered < required:
            raise ValueError(
                f"category {row.category_id} offers {offered} credits but requires "
                f"{required} -- unsatisfiable"
            )

        distinct = sorted({int(c) for c in group.credits})
        if len(distinct) != 1 or required % distinct[0] != 0:
            raise ValueError(
                f"category {row.category_id} offers {offered} credits against a "
                f"{required}-credit requirement, but its courses carry mixed credits "
                f"{distinct} -- 'any N courses' is not well defined here"
            )
        rules[row.category_id] = ("ANY_N", required // distinct[0])

    return rules


def verify_against_catalogue(rules: dict[str, tuple[str, int | None]]) -> None:
    """Cross-check the derived rules against what the Catalogue says."""
    problems = []
    for category_id, derived in sorted(rules.items()):
        suffix = category_id.rsplit("-", 1)[-1]
        expected = EXPECTED_RULES.get(suffix)
        if expected is None:
            problems.append(f"{category_id}: unrecognised category suffix {suffix!r}")
        elif derived != expected:
            problems.append(f"{category_id}: derived {derived}, Catalogue says {expected}")
    if problems:
        raise SystemExit(
            "Derived selection rules disagree with the Catalogue:\n  "
            + "\n  ".join(problems)
        )
    print(f"  selection rules: derived and cross-checked for {len(rules)} categories")


def load(xlsx: Path) -> None:
    settings = get_settings()
    sheets = pd.read_excel(xlsx, sheet_name=None)
    print(f"Reading {xlsx.name}: {len(sheets)} sheets")

    terms = sheets["Terms"]
    reqs = sheets["Program_Requirements"]
    courses = sheets["Courses"]
    category_courses = sheets["Category_Courses"]
    prereqs = sheets["Course_Prerequisites"]
    students = sheets["Students"]
    enrollments = sheets["Enrollments"]
    schedule = sheets["Class_Schedule_FA2026"]
    grading = sheets["Grading_Scale"]

    rules = derive_selection_rules(reqs, category_courses, courses)
    verify_against_catalogue(rules)

    with SessionLocal() as session:
        # Wipe first so a re-run is a replace, not a duplicate-key crash.
        for model in DELETE_ORDER:
            session.execute(delete(model))

        session.execute(
            insert(Term),
            [
                {
                    "term_code": r.term_code,
                    "term_name": r.term_name,
                    "start_date": pd.to_datetime(r.start_date).date(),
                    "end_date": pd.to_datetime(r.end_date).date(),
                }
                for r in terms.itertuples()
            ],
        )

        # Program_Requirements is denormalised: programme columns repeat per
        # category row, so the programme is the deduplicated projection of it.
        programs = reqs[
            ["program_code", "program_name", "total_credits_required"]
        ].drop_duplicates()
        session.execute(
            insert(Program),
            [
                {
                    "program_code": r.program_code,
                    "program_name": r.program_name,
                    "total_credits_required": int(r.total_credits_required),
                }
                for r in programs.itertuples()
            ],
        )

        session.execute(
            insert(GradingScale),
            [
                {
                    "grade": r.grade,
                    # NaN for W and P, which carry no points at all.
                    "grade_points": (
                        None
                        if pd.isna(r.grade_points)
                        # via str so 3.7 stays 3.7 rather than 3.7000000000000002
                        else Decimal(str(r.grade_points))
                    ),
                    "earns_credit": bool(r.earns_credit),
                    "included_in_gpa": bool(r.included_in_gpa),
                }
                for r in grading.assign(
                    earns_credit=yes_no_to_bool(grading.earns_credit),
                    included_in_gpa=yes_no_to_bool(grading.included_in_gpa),
                ).itertuples()
            ],
        )

        session.execute(
            insert(Course),
            [
                {
                    "course_code": r.course_code,
                    "title": r.title,
                    "credits": int(r.credits),
                    "description": r.description,
                }
                for r in courses.itertuples()
            ],
        )

        session.execute(
            insert(ProgramRequirementCategory),
            [
                {
                    "category_id": r.category_id,
                    "program_code": r.program_code,
                    "category_name": r.category_name,
                    "credits_required": int(r.credits_required),
                    "selection_rule": rules[r.category_id][0],
                    "courses_required": rules[r.category_id][1],
                    "min_grade_points": MIN_GRADE_POINTS_BY_SUFFIX[
                        r.category_id.rsplit("-", 1)[-1]
                    ],
                }
                for r in reqs.itertuples()
            ],
        )

        session.execute(
            insert(CategoryCourse),
            [
                {"category_id": r.category_id, "course_code": r.course_code}
                for r in category_courses.itertuples()
            ],
        )

        session.execute(
            insert(CoursePrerequisite),
            [
                {
                    "course_code": r.course_code,
                    "prerequisite_course_code": r.prerequisite_course_code,
                }
                for r in prereqs.itertuples()
            ],
        )

        session.execute(
            insert(Student),
            [
                {
                    "student_id": r.student_id,
                    "first_name": r.first_name,
                    "last_name": r.last_name,
                    "email": r.email,
                    "program_code": r.program_code,
                    "entry_term": r.entry_term,
                    "expected_graduation_term": r.expected_graduation_term,
                    "academic_status": r.academic_status,
                    "advisor_name": r.advisor_name,
                    "scenario_note": getattr(r, "scenario_note", None),
                }
                for r in students.itertuples()
            ],
        )

        session.execute(
            insert(Enrollment),
            [
                {
                    "student_id": r.student_id,
                    "term_code": r.term_code,
                    "course_code": r.course_code,
                    "credits": int(r.credits),
                    # Blank grade means in progress; the CHECK constraint on the
                    # table enforces that pairing.
                    "grade": None if pd.isna(r.grade) else r.grade,
                    "status": r.status,
                }
                for r in enrollments.itertuples()
            ],
        )

        # The sheet is named for its term and carries no term column, so the term
        # comes from configuration. course_title/credits are dropped: they
        # duplicate `courses` exactly (verified), and two sources for one fact is
        # one source too many.
        session.execute(
            insert(ClassSchedule),
            [
                {
                    "term_code": settings.current_term,
                    "course_code": r.course_code,
                    "days": r.days,
                    "start_time": to_time(r.start_time),
                    "end_time": to_time(r.end_time),
                    "room": r.room,
                    "instructor": r.instructor,
                }
                for r in schedule.itertuples()
            ],
        )

        session.commit()

    report(xlsx)


def report(xlsx: Path) -> None:
    """Print loaded row counts beside the source sheet sizes."""
    sheet_for = {
        Term: "Terms",
        Program: None,  # derived from Program_Requirements
        GradingScale: "Grading_Scale",
        Course: "Courses",
        ProgramRequirementCategory: "Program_Requirements",
        CategoryCourse: "Category_Courses",
        CoursePrerequisite: "Course_Prerequisites",
        Student: "Students",
        Enrollment: "Enrollments",
        ClassSchedule: "Class_Schedule_FA2026",
    }
    sizes = {n: len(df) for n, df in pd.read_excel(xlsx, sheet_name=None).items()}

    print("\nLoaded:")
    mismatches = 0
    with SessionLocal() as session:
        for model, sheet in sheet_for.items():
            n = session.execute(
                select(func.count()).select_from(model.__table__)
            ).scalar_one()
            if sheet is None:
                print(f"  {model.__tablename__:<32} {n:>4}")
                continue
            expected = sizes[sheet]
            ok = n == expected
            mismatches += not ok
            flag = "" if ok else f"  <-- MISMATCH, sheet has {expected}"
            print(f"  {model.__tablename__:<32} {n:>4}  (sheet {sheet}: {expected}){flag}")

    if mismatches:
        raise SystemExit(f"\n{mismatches} table(s) do not match their source sheet")
    print("\nAll tables match their source sheet row counts.")


if __name__ == "__main__":
    path = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else get_settings().data_dir / WORKBOOK_NAME
    )
    if not path.exists():
        raise SystemExit(
            f"workbook not found: {path}\n"
            "Set DATA_DIR, or pass the path as an argument."
        )
    load(path)
