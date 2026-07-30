"""Cross-check the SQL academic queries against an independent computation.

    docker compose exec backend python scripts/verify_phase1.py

The point is independence, which is what makes this worth more than a test that
restates the query. The reference implementation here:

  * reads the spreadsheet directly, so it never touches the schema or the loader,
  * is written with dicts and loops rather than set-based SQL, so a mistake in one
    is unlikely to be mirrored in the other,
  * restates the Handbook policy from the documents (C- gate, W/P handling,
    repeats) instead of reading `min_grade_points` / `selection_rule` out of the
    database, so it also checks that the loader seeded those correctly.

Exits non-zero on any disagreement, so it can gate later phases.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from app.academics import (
    format_progress_table,
    format_summary,
    get_academic_summary,
    get_degree_progress,
    is_graduation_credit_complete,
)
from app.config import get_settings
from app.db import SessionLocal

WORKBOOK_NAME = "Eurisko_University_Data.xlsx"

STUDENTS = {
    "S2023011": "Maya Haddad -- 4th year, on track, one term from graduating",
    "S2023027": "Jad Mansour -- the credit-count trap",
    "S2024019": "Karim Nassar -- ordinary progression",
    "S2025008": "Rania Khoury -- probation, repeating two failed courses",
    "S2026042": "Lynn Abou Chakra -- first term, empty history",
}

# Handbook policy, restated here from the documents rather than read from the
# database, so this script also verifies the loader encoded it correctly.
C_MINUS = Decimal("1.7")
CATEGORY_MIN_POINTS = {"MAJ": C_MINUS}


def best_attempts(sheets: dict[str, pd.DataFrame], student_id: str) -> dict[str, dict]:
    """Highest completed attempt per course, as a plain dict.

    The Handbook's repeat rule: a course may be attempted more than once, only the
    higher grade counts toward GPA, and credit is earned once.
    """
    grading = sheets["Grading_Scale"].set_index("grade")
    mine = sheets["Enrollments"].query(
        "student_id == @student_id and status == 'Completed'"
    )

    best: dict[str, dict] = {}
    for row in mine.itertuples():
        points = grading.loc[row.grade, "grade_points"]
        # W and P have no points; rank them below any real grade.
        rank = Decimal("-1") if pd.isna(points) else Decimal(str(points))
        current = best.get(row.course_code)
        if current is None or rank > current["rank"]:
            best[row.course_code] = {
                "rank": rank,
                "grade": row.grade,
                "credits": int(row.credits),
                "points": None if pd.isna(points) else Decimal(str(points)),
                "earns_credit": grading.loc[row.grade, "earns_credit"] == "Yes",
                "in_gpa": grading.loc[row.grade, "included_in_gpa"] == "Yes",
            }
    return best


def reference_summary(sheets: dict[str, pd.DataFrame], student_id: str) -> dict:
    best = best_attempts(sheets, student_id)

    quality_points = Decimal(0)
    gpa_credits = 0
    credits_earned = 0
    for entry in best.values():
        if entry["in_gpa"]:
            quality_points += entry["points"] * entry["credits"]
            gpa_credits += entry["credits"]
        if entry["earns_credit"]:
            credits_earned += entry["credits"]

    in_progress = sheets["Enrollments"].query(
        "student_id == @student_id and status == 'In progress'"
    )

    return {
        "quality_points": quality_points,
        "gpa_credits": gpa_credits,
        "gpa": (
            (quality_points / gpa_credits).quantize(Decimal("0.01"))
            if gpa_credits
            else None
        ),
        "credits_earned": credits_earned,
        "credits_in_progress": int(in_progress.credits.sum()),
    }


def reference_progress(sheets: dict[str, pd.DataFrame], student_id: str) -> list[dict]:
    program = sheets["Students"].set_index("student_id").loc[student_id, "program_code"]
    reqs = sheets["Program_Requirements"].query("program_code == @program")
    members = sheets["Category_Courses"]
    course_credits = sheets["Courses"].set_index("course_code").credits.to_dict()
    best = best_attempts(sheets, student_id)

    in_progress = set(
        sheets["Enrollments"]
        .query("student_id == @student_id and status == 'In progress'")
        .course_code
    )

    out = []
    for cat in reqs.sort_values("category_id").itertuples():
        offered = list(members.query("category_id == @cat.category_id").course_code)
        floor = CATEGORY_MIN_POINTS.get(cat.category_id.rsplit("-", 1)[-1])

        counted = []
        for code in offered:
            entry = best.get(code)
            if entry is None or not entry["earns_credit"]:
                continue
            if floor is not None and (entry["points"] is None or entry["points"] < floor):
                continue
            counted.append(code)

        credits_counted = sum(course_credits[c] for c in counted)
        required = int(cat.credits_required)

        out.append(
            {
                "category_id": cat.category_id,
                "courses_offered": len(offered),
                "courses_counted": len(counted),
                "credits_counted": credits_counted,
                # Surplus never carries: cap at the requirement.
                "credits_applied": min(credits_counted, required),
                "credits_required": required,
                "is_satisfied": credits_counted >= required,
                "credits_in_progress": sum(
                    course_credits[c] for c in offered if c in in_progress
                ),
            }
        )
    return out


def check_unexercised_rules(session) -> list[str]:
    """Prove the two rules the real dataset never exercises.

    Neither the credit cap nor the repeat rule can be validated against the five
    students: nobody has completed more courses than an ANY_N category requires,
    and nobody has *passed* a course twice (Rania's two repeats are still in
    progress, so each has exactly one completed attempt). Both rules would
    therefore pass the 5-student matrix even if implemented wrongly.

    So this fabricates a student who exercises both, checks the answers, and rolls
    back. A nested transaction keeps the frozen dataset untouched -- nothing is
    committed, whatever the outcome.
    """
    problems: list[str] = []
    sid = "SVERIFY01"

    with session.begin_nested() as nested:
        session.execute(
            text(
                "INSERT INTO students (student_id, first_name, last_name, email,"
                " program_code, entry_term, expected_graduation_term,"
                " academic_status, advisor_name)"
                " VALUES (:sid, 'Cap', 'Repeat', :email, 'BE-CENG', 'FA2023',"
                " 'SP2027', 'Good standing', 'Dr. Nadim Fares')"
            ),
            {"sid": sid, "email": f"{sid}@verify.invalid"},
        )
        rows = [
            # All four General Education courses passed: 12 credits offered
            # against a 9-credit, any-3 requirement. Exercises the cap.
            ("FA2023", "ENGL 101", 3, "A"),
            ("FA2023", "ENGL 201", 3, "A"),
            ("FA2023", "HUMN 210", 3, "A"),
            ("FA2023", "SOCI 240", 3, "A"),
            # MATH 101 passed at C, then repeated and passed at A. Exercises the
            # repeat rule in both directions: only the higher grade counts toward
            # GPA, and the credit is earned once, not twice.
            ("FA2023", "MATH 101", 3, "C"),
            ("SP2024", "MATH 101", 3, "A"),
        ]
        for term, course, credits, grade in rows:
            session.execute(
                text(
                    "INSERT INTO enrollments (student_id, term_code, course_code,"
                    " credits, grade, status)"
                    " VALUES (:sid, :term, :course, :credits, :grade, 'Completed')"
                ),
                {"sid": sid, "term": term, "course": course,
                 "credits": credits, "grade": grade},
            )

        summary = get_academic_summary(session, sid)
        progress = {r["category_id"]: r for r in get_degree_progress(session, sid)}

        # Five distinct courses, all counted at A: 5 x 4.0 x 3 = 60 quality points
        # over 15 credits. Counting the superseded C too would give 66/18 = 3.67.
        expected_summary = {
            "quality_points": Decimal("60.00"),
            "gpa_credits": 15,
            "gpa": Decimal("4.00"),
            "credits_earned": 15,
        }
        for field, want in expected_summary.items():
            if not same(summary[field], want):
                problems.append(
                    f"repeat rule: summary.{field} = {summary[field]!r}, expected {want!r}"
                )

        gen = progress["BE-CENG-GEN"]
        # 12 credits held, 9 applied. If the cap were missing, credits_applied
        # would read 12 and three credits would silently offset another category.
        for field, want in {
            "courses_counted": 4,
            "credits_counted": 12,
            "credits_applied": 9,
            "credits_remaining": 0,
            "is_satisfied": True,
        }.items():
            if not same(gen[field], want):
                problems.append(
                    f"credit cap: BE-CENG-GEN.{field} = {gen[field]!r}, expected {want!r}"
                )

        core = progress["BE-CENG-CORE"]
        for field, want in {"courses_counted": 1, "credits_counted": 3}.items():
            if not same(core[field], want):
                problems.append(
                    f"repeat rule: BE-CENG-CORE.{field} = {core[field]!r}, "
                    f"expected {want!r}"
                )

        nested.rollback()

    # Belt and braces: confirm the fabricated student really is gone.
    leaked = session.execute(
        text("SELECT count(*) FROM students WHERE student_id = :sid"), {"sid": sid}
    ).scalar_one()
    if leaked:
        problems.append(f"fabricated student {sid} was not rolled back")

    return problems


COMPARE_SUMMARY = ["quality_points", "gpa_credits", "gpa", "credits_earned",
                   "credits_in_progress"]
COMPARE_PROGRESS = ["courses_offered", "courses_counted", "credits_counted",
                    "credits_applied", "credits_required", "is_satisfied",
                    "credits_in_progress"]


def same(a, b) -> bool:
    """Compare across Decimal / int / bool / None without tripping on type.

    Booleans are checked before numbers: Decimal(str(True)) raises, and treating
    True as 1 would let a boolean field silently compare equal to an int.
    """
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) is bool(b)
    return Decimal(str(a)) == Decimal(str(b))


def main() -> int:
    xlsx = get_settings().data_dir / WORKBOOK_NAME
    if not xlsx.exists():
        raise SystemExit(f"workbook not found: {xlsx}")
    sheets = pd.read_excel(xlsx, sheet_name=None)

    failures: list[str] = []

    with SessionLocal() as session:
        for student_id, description in STUDENTS.items():
            print(f"\n{'=' * 78}\n{student_id}  {description}\n{'=' * 78}")

            sql_summary = get_academic_summary(session, student_id)
            ref_summary = reference_summary(sheets, student_id)
            print(f"  SQL      {format_summary(sql_summary).strip()}")
            print(f"  pandas   {format_summary(ref_summary).strip()}")
            for field in COMPARE_SUMMARY:
                if not same(sql_summary[field], ref_summary[field]):
                    failures.append(
                        f"{student_id} summary.{field}: SQL={sql_summary[field]!r} "
                        f"pandas={ref_summary[field]!r}"
                    )

            sql_progress = get_degree_progress(session, student_id)
            ref_progress = reference_progress(sheets, student_id)
            print("\n" + format_progress_table(sql_progress))

            if len(sql_progress) != len(ref_progress):
                failures.append(
                    f"{student_id}: SQL returned {len(sql_progress)} categories, "
                    f"pandas {len(ref_progress)}"
                )
            else:
                by_id = {r["category_id"]: r for r in ref_progress}
                for row in sql_progress:
                    ref = by_id[row["category_id"]]
                    for field in COMPARE_PROGRESS:
                        if not same(row[field], ref[field]):
                            failures.append(
                                f"{student_id} {row['category_id']}.{field}: "
                                f"SQL={row[field]!r} pandas={ref[field]!r}"
                            )

            # Why a credit total is the wrong lens. Not "the two rules disagree
            # about MET/not met" -- no student here is at 72 yet, so that
            # comparison would agree trivially for all five and prove nothing.
            # The point is that a headline percentage hides *which* requirements
            # are outstanding: Jad reads as three-quarters done while the entire
            # capstone category is untouched.
            total_required = int(
                sheets["Program_Requirements"]
                .set_index("category_id")
                .loc[sql_progress[0]["category_id"], "total_credits_required"]
            )
            earned = sql_summary["credits_earned"]
            satisfied = sum(1 for r in sql_progress if r["is_satisfied"])
            unmet = [
                f"{r['category_name']} ({r['credits_remaining']}cr)"
                for r in sql_progress
                if not r["is_satisfied"]
            ]
            print(
                f"\n  by credits:    {earned}/{total_required} "
                f"({earned * 100 // total_required}% complete)"
                f"\n  by categories: {satisfied}/{len(sql_progress)} satisfied"
                f"  ->  all requirements met: "
                f"{'YES' if is_graduation_credit_complete(sql_progress) else 'NO'}"
                + (f"\n  outstanding:   {'; '.join(unmet)}" if unmet else "")
            )

        print(f"\n{'=' * 78}\nRules the 5-student dataset cannot exercise\n{'=' * 78}")
        rule_problems = check_unexercised_rules(session)
        if rule_problems:
            failures.extend(rule_problems)
            print("  FAILED (see below)")
        else:
            print("  credit capping and the repeat rule both verified against a "
                  "fabricated\n  student, rolled back afterwards.")

    print(f"\n{'=' * 78}")
    if failures:
        print(f"{len(failures)} DISAGREEMENT(S) between SQL and the reference:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SQL matches the independent reference for all 5 students, "
          "on GPA, credits and every requirement category.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
