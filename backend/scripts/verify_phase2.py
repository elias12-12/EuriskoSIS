"""Exercise every Phase 2 endpoint for every student, over real HTTP.

    docker compose exec backend python scripts/verify_phase2.py

Deliberately goes through the HTTP surface rather than calling the query functions
directly: Phase 1 already verified the SQL against an independent implementation,
so what is unverified here is the layer on top -- routing, response schemas,
serialisation, and the 404 paths.

The GPA and credit figures are asserted against the values hand-verified in
Phase 1, so this doubles as a regression guard: if a later refactor quietly
changes how credits are counted, these numbers move and the script fails.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import Any

import httpx

BASE = "http://localhost:8000"

# Hand-verified in Phase 1 (see DESIGN.md). Treated as fixed expectations.
EXPECTED: dict[str, dict[str, Any]] = {
    "S2023011": {"name": "Maya Haddad", "gpa": Decimal("3.62"), "earned": 66,
                 "in_progress": 6, "satisfied": 3, "status": "Good standing"},
    "S2023027": {"name": "Jad Mansour", "gpa": Decimal("2.78"), "earned": 55,
                 "in_progress": 9, "satisfied": 2, "status": "Good standing"},
    "S2024019": {"name": "Karim Nassar", "gpa": Decimal("2.96"), "earned": 49,
                 "in_progress": 9, "satisfied": 2, "status": "Good standing"},
    "S2025008": {"name": "Rania Khoury", "gpa": Decimal("1.65"), "earned": 19,
                 "in_progress": 9, "satisfied": 1, "status": "Academic probation"},
    "S2026042": {"name": "Lynn Abou Chakra", "gpa": None, "earned": 0,
                 "in_progress": 13, "satisfied": 0, "status": "Good standing"},
}

# Courses chosen to hit distinct rules: a MECH chain course, a CENG chain course,
# a no-prerequisite course, an elective with two prerequisites, and the capstone.
ELIGIBILITY_COURSES = [
    "MECH 310",  # prereq MECH 210 -- the brief's hybrid example
    "MECH 330",  # prereq MECH 200
    "CENG 320",  # prereq CENG 310
    "ENGR 450",  # two prereqs: CMPS 101 AND MATH 301
    "ENGR 490",  # capstone: ENGL 201 AND ENGR 230
    "MATH 101",  # no prerequisites
    "CHEM 101",  # no prerequisites, not offered in FA2026? (checks that path)
]

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failures.append(f"{label}{': ' + detail if detail else ''}")


def dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=30) as client:
        for student_id, want in EXPECTED.items():
            print(f"\n{'=' * 78}\n{student_id}  {want['name']}\n{'=' * 78}")

            # --- profile -----------------------------------------------------
            r = client.get(f"/students/{student_id}/profile")
            check(f"{student_id} profile status", r.status_code == 200, str(r.status_code))
            profile = r.json()
            check(
                f"{student_id} profile name",
                f"{profile['first_name']} {profile['last_name']}" == want["name"],
            )
            check(
                f"{student_id} profile status field",
                profile["academic_status"] == want["status"],
                profile["academic_status"],
            )
            check(
                f"{student_id} profile gpa",
                dec(profile["academics"]["gpa"]) == want["gpa"],
                f"got {profile['academics']['gpa']}, want {want['gpa']}",
            )
            check(
                f"{student_id} profile credits_earned",
                profile["academics"]["credits_earned"] == want["earned"],
                str(profile["academics"]["credits_earned"]),
            )
            print(
                f"  profile          {profile['program_code']} | "
                f"{profile['academic_status']} | GPA "
                f"{profile['academics']['gpa'] or 'n/a'} | "
                f"{profile['academics']['credits_earned']} earned | "
                f"advisor {profile['advisor_name']}"
            )

            # --- schedule ----------------------------------------------------
            schedule = client.get(f"/students/{student_id}/schedule").json()
            check(
                f"{student_id} schedule credits match in-progress",
                schedule["total_credits"] == want["in_progress"],
                f"schedule {schedule['total_credits']} vs "
                f"academics {want['in_progress']}",
            )
            print(
                f"  schedule         {len(schedule['classes'])} classes, "
                f"{schedule['total_credits']} credits in {schedule['term_code']}"
            )
            for cls in schedule["classes"]:
                print(
                    f"                     {cls['course_code']:<9} "
                    f"{cls['days']:<9} {cls['start_time'][:5]}-{cls['end_time'][:5]} "
                    f"{cls['room']:<9} {cls['instructor']}"
                )

            # --- courses -----------------------------------------------------
            history = client.get(f"/students/{student_id}/courses").json()
            completed = [c for c in history["courses"] if c["status"] == "Completed"]
            in_prog = [c for c in history["courses"] if c["status"] == "In progress"]
            check(
                f"{student_id} every in-progress course has null grade",
                all(c["grade"] is None for c in in_prog),
            )
            check(
                f"{student_id} every completed course has a grade",
                all(c["grade"] is not None for c in completed),
            )
            check(
                f"{student_id} history in-progress credits",
                sum(c["credits"] for c in in_prog) == want["in_progress"],
            )
            print(
                f"  courses          {len(history['courses'])} rows "
                f"({len(completed)} completed, {len(in_prog)} in progress)"
            )

            # --- degree progress ---------------------------------------------
            progress = client.get(f"/students/{student_id}/degree-progress").json()
            satisfied = [c for c in progress["categories"] if c["is_satisfied"]]
            check(
                f"{student_id} categories satisfied",
                len(satisfied) == want["satisfied"],
                f"got {len(satisfied)}, want {want['satisfied']}",
            )
            check(
                f"{student_id} all_categories_satisfied consistent",
                progress["all_categories_satisfied"]
                == (len(progress["unsatisfied_categories"]) == 0),
            )
            check(
                f"{student_id} five categories returned",
                len(progress["categories"]) == 5,
                str(len(progress["categories"])),
            )
            # The capping invariant: applied credits can never exceed what the
            # category requires, however many surplus credits are held.
            for cat in progress["categories"]:
                check(
                    f"{student_id} {cat['category_id']} applied <= required",
                    cat["credits_applied"] <= cat["credits_required"],
                    f"{cat['credits_applied']} > {cat['credits_required']}",
                )
                check(
                    f"{student_id} {cat['category_id']} applied <= counted",
                    cat["credits_applied"] <= cat["credits_counted"],
                )
            print(
                f"  degree-progress  {len(satisfied)}/5 categories satisfied; "
                f"all met: {progress['all_categories_satisfied']}"
            )
            if progress["unsatisfied_categories"]:
                print(
                    f"                     outstanding: "
                    f"{', '.join(progress['unsatisfied_categories'])}"
                )

            # --- eligibility matrix ------------------------------------------
            print("  eligibility")
            for course in ELIGIBILITY_COURSES:
                resp = client.get(
                    f"/students/{student_id}/eligibility/{course}"
                )
                check(
                    f"{student_id} eligibility {course} status",
                    resp.status_code == 200,
                    str(resp.status_code),
                )
                if resp.status_code != 200:
                    continue
                e = resp.json()
                # A blocked result must always say why, and an eligible one must
                # have no unsatisfied reason. Otherwise the endpoint is useless to
                # a student and unciteable by the agent.
                blockers = [x for x in e["reasons"] if not x["satisfied"]]
                check(
                    f"{student_id} {course} ineligible implies a stated reason",
                    e["eligible"] or bool(blockers),
                )
                check(
                    f"{student_id} {course} eligible implies no blockers",
                    (not e["eligible"]) or not blockers,
                )
                # Registering for a course you already hold a seat in cannot change
                # your load. Getting this wrong overstates the prospective total by
                # the course's credits and can invent a spurious load blocker.
                if e["already_registered"]:
                    check(
                        f"{student_id} {course} already-registered load unchanged",
                        e["prospective_credits"] == e["registered_credits"],
                        f"{e['registered_credits']} -> {e['prospective_credits']}",
                    )
                    check(
                        f"{student_id} {course} already-registered has no load blocker",
                        not any(b["rule"] == "course_load" for b in blockers),
                    )
                else:
                    check(
                        f"{student_id} {course} prospective = registered + credits",
                        e["prospective_credits"]
                        == e["registered_credits"] + e["credits"],
                    )
                verdict = "YES" if e["eligible"] else "no "
                if e["requires_advisor_approval"]:
                    verdict = "YES*"
                summary = "; ".join(b["rule"] for b in blockers) or "-"
                print(f"    {course:<10} {verdict:<4} {summary}")

        # --- error paths -----------------------------------------------------
        print(f"\n{'=' * 78}\nError paths\n{'=' * 78}")
        for path, want_status, label in [
            ("/students/NOPE999/profile", 404, "unknown student profile"),
            ("/students/NOPE999/schedule", 404, "unknown student schedule"),
            ("/students/NOPE999/courses", 404, "unknown student courses"),
            ("/students/NOPE999/degree-progress", 404, "unknown student progress"),
            ("/students/NOPE999/eligibility/MATH 101", 404, "unknown student elig"),
            ("/students/S2023011/eligibility/FAKE 999", 404, "unknown course"),
        ]:
            got = client.get(path).status_code
            check(f"{label} -> {want_status}", got == want_status, f"got {got}")
            print(f"  {label:<32} {got}")

        # Swagger and the schema must actually render -- the Phase 2 exit check is
        # stated in terms of hitting endpoints through Swagger UI.
        for path in ("/docs", "/openapi.json"):
            got = client.get(path).status_code
            check(f"{path} renders", got == 200, f"got {got}")
            print(f"  {path:<32} {got}")

    print(f"\n{'=' * 78}")
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All Phase 2 endpoints correct for all 5 students, including error paths.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
