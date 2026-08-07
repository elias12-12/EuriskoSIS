"""Phase 6 exit check: an admin setting change alters the very next chat response.

    docker compose exec backend python scripts/verify_phase6.py --structural
    docker compose exec backend python scripts/verify_phase6.py

PROJECT_PLAN Phase 6's exit check is one sentence -- "confirm changing a
behaviour setting changes the very next chat response, with no restart" -- and it
is really a claim about *where the settings are read*, not about the form that
writes them. `assistant_config.load` runs at the start of every turn and is
deliberately not cached; this proves it.

Part 1 (`--structural`) needs the database and API but no key or model:

- the admin login works, and a wrong password does not;
- **an admin token cannot reach `/me/*`, and a student token cannot reach
  `/admin/*`.** They are separate tables and separate dependencies, and this is
  the test that they have not quietly been merged. A single sessions table with a
  nullable owner column would fail here;
- settings survive a write and read back;
- the browsers filter, and their totals reflect the filter rather than the table.

Part 2 needs a key: two chat turns either side of a settings change, with the
reply length compared. Length rather than tone, because "did this get shorter"
is measurable and "did this get friendlier" is not.
"""

from __future__ import annotations

import argparse
import sys

import httpx

BASE = "http://localhost:8000"
ADMIN_PASSWORD = "eurisko_admin"  # the compose default; override with --password
STUDENT = "S2023011"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"   {'PASS' if ok else 'FAIL'}  {label}" + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}{': ' + detail if detail else ''}")


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def structural_checks(client: httpx.Client, password: str) -> tuple[str, str]:
    print(f"\n{'=' * 78}\nPart 1: two principals, and the settings round-trip\n{'=' * 78}")

    check(
        "a wrong administrator password is refused",
        client.post("/admin/login", json={"password": "not-the-password"}).status_code
        == 401,
    )

    response = client.post("/admin/login", json={"password": password})
    check("the administrator can log in", response.status_code == 200, response.text[:200])
    if response.status_code != 200:
        raise SystemExit("cannot continue without an admin session")
    admin_token = response.json()["access_token"]
    check(
        "an admin session has no student_id",
        response.json()["student_id"] is None,
    )

    student_token = client.post("/auth/login", json={"student_id": STUDENT}).json()[
        "access_token"
    ]

    # The check that matters: the two principals are not interchangeable.
    print("\n   -- cross-principal --")
    for path in ("/admin/settings", "/admin/students", "/admin/documents"):
        check(
            f"a student token is refused by {path}",
            client.get(path, headers=auth(student_token)).status_code == 401,
        )
    for path in ("/me/profile", "/me/schedule", "/me/degree-progress"):
        check(
            f"an admin token is refused by {path}",
            client.get(path, headers=auth(admin_token)).status_code == 401,
        )
    check(
        "unauthenticated access to /admin is refused",
        client.get("/admin/settings").status_code == 401,
    )

    # --- settings round-trip ------------------------------------------------
    print("\n   -- settings --")
    original = client.get("/admin/settings", headers=auth(admin_token)).json()
    check("settings can be read", "tone" in original, str(original)[:160])

    changed = {**original, "tone": "formal", "response_length": "brief"}
    saved = client.put("/admin/settings", json=changed, headers=auth(admin_token))
    check("settings can be written", saved.status_code == 200, saved.text[:200])
    reread = client.get("/admin/settings", headers=auth(admin_token)).json()
    check(
        "the write is durable",
        reread["tone"] == "formal" and reread["response_length"] == "brief",
        str(reread),
    )

    rejected = client.put(
        "/admin/settings",
        json={**original, "tone": "sarcastic"},
        headers=auth(admin_token),
    )
    check(
        "an unknown tone is rejected with 422, not a 500",
        rejected.status_code == 422,
        str(rejected.status_code),
    )

    # Restore, so running this script does not leave the stack reconfigured.
    client.put("/admin/settings", json=original, headers=auth(admin_token))

    # --- browsers -----------------------------------------------------------
    print("\n   -- browsers --")
    everyone = client.get("/admin/students", headers=auth(admin_token)).json()
    check("the student browser lists all five", everyone["total"] == 5, str(everyone["total"]))

    filtered = client.get(
        "/admin/students?academic_status=Academic probation", headers=auth(admin_token)
    ).json()
    check(
        "filtering narrows the total, not just the page",
        filtered["total"] == 1 and filtered["items"][0]["student_id"] == "S2025008",
        f"total={filtered['total']}",
    )

    courses = client.get("/admin/courses?subject=MECH", headers=auth(admin_token)).json()
    check(
        "the course browser filters by subject",
        courses["total"] == 6,
        f"expected 6 MECH courses, got {courses['total']}",
    )
    check(
        "prerequisites are aggregated into the row",
        any(row["prerequisites"] for row in courses["items"]),
    )

    enrollments = client.get(
        f"/admin/enrollments?student_id={STUDENT}", headers=auth(admin_token)
    ).json()
    check(
        "the enrollment browser filters by student",
        enrollments["total"] > 0
        and all(row["student_id"] == STUDENT for row in enrollments["items"]),
    )

    options = client.get("/admin/filters", headers=auth(admin_token)).json()
    check(
        "filter options are queried from the data",
        "Academic probation" in options["academic_statuses"]
        and "MECH" in options["subjects"],
        str(options)[:200],
    )

    return admin_token, student_token


def behavioural_checks(client: httpx.Client, admin_token: str, student_token: str) -> None:
    print(f"\n{'=' * 78}\nPart 2: a setting change alters the next reply\n{'=' * 78}")

    original = client.get("/admin/settings", headers=auth(admin_token)).json()
    question = "What are the requirements to graduate?"

    def ask() -> str:
        response = client.post(
            "/me/chat", json={"message": question}, headers=auth(student_token), timeout=120
        )
        if response.status_code != 200:
            failures.append(f"chat {response.status_code}: {response.text[:200]}")
            return ""
        return response.json()["reply"]

    try:
        client.put(
            "/admin/settings",
            json={**original, "response_length": "brief"},
            headers=auth(admin_token),
        )
        brief = ask()
        print(f"   brief    ({len(brief):>4} chars): {brief[:120]}...")

        client.put(
            "/admin/settings",
            json={**original, "response_length": "detailed"},
            headers=auth(admin_token),
        )
        detailed = ask()
        print(f"   detailed ({len(detailed):>4} chars): {detailed[:120]}...")

        # No restart happened between those two calls, which is the whole claim.
        check(
            "the detailed answer is materially longer than the brief one",
            len(detailed) > len(brief) * 1.3,
            f"brief={len(brief)}, detailed={len(detailed)}",
        )
        check("both answers are non-empty", bool(brief) and bool(detailed))
        check(
            "the grounded answer cites a source",
            "Handbook" in detailed or "section" in detailed.lower(),
            detailed[:200],
        )
    finally:
        client.put("/admin/settings", json=original, headers=auth(admin_token))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structural", action="store_true", help="skip the chat tests")
    parser.add_argument("--password", default=ADMIN_PASSWORD)
    args = parser.parse_args()

    try:
        with httpx.Client(base_url=BASE, timeout=30) as client:
            admin_token, student_token = structural_checks(client, args.password)
            if not args.structural:
                behavioural_checks(client, admin_token, student_token)
    except httpx.HTTPError as exc:
        print(f"\nCould not reach the API at {BASE}: {exc}", file=sys.stderr)
        print("Is the stack up? `docker compose up -d`", file=sys.stderr)
        return 2

    print(f"\n{'=' * 78}")
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("PASSED: principals stay separate, and settings take effect immediately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
