"""Phase 4 exit check: does the authenticated identity actually scope everything?

    docker compose exec backend python scripts/verify_phase4.py --structural
    docker compose exec backend python scripts/verify_phase4.py

PROJECT_PLAN Phase 4's exit check is: ask "what's my schedule" as two different
students in two different sessions and confirm each gets only their own data,
then try to break it by asking one about the other's record and confirm it
refuses.

This script runs that in two parts, for a reason worth stating.

**Part 1 is structural and needs no API key, no model and no database.** It
inspects the JSON schemas the agent hands the model and asserts that no scoped
tool has a parameter capable of naming a student. This is the real guarantee: a
prompt injection cannot fill in a field that does not exist. Prompt instructions
about refusing are a courtesy that produces a good refusal message; they are not
the control, and a test that only checked the model's *behaviour* would be
testing the courtesy while leaving the control unverified.

**Part 2 is behavioural and needs the running stack.** Two live sessions, real
model calls, and the cross-student question from the plan. It checks the quality
of the refusal, which part 1 cannot.

Part 1 failing is a design regression. Part 2 failing is a prompt problem. They
have different fixes, so they are reported separately.
"""

from __future__ import annotations

import argparse
import sys

import httpx

BASE = "http://localhost:8000"

# Tools that return the student's own record and take nothing at all. The
# strongest form of the guarantee: there is no argument to fill in.
NO_PARAM_TOOLS = ("get_my_schedule", "get_my_courses", "get_my_degree_progress")

# Scoped tools that legitimately take arguments -- a course code, a reason, a
# time. They must still expose no way to name a *student*, and no way to name a
# *conversation*: `confirm_advisor_appointment` proves consent by reading the
# stored history of `ctx.deps.conversation_id`, so a model-supplied one would let
# it point the check at a conversation where a proposal did happen (Phase 5).
SCOPED_TOOLS_WITH_ARGS = (
    "check_course_eligibility",
    "request_advisor_appointment",
    "confirm_advisor_appointment",
)

# Not scoped, correctly: the corpus is institutional policy, identical for all.
UNSCOPED_TOOLS = ("search_documents",)

# Anything that looks like a way to name a student or a thread. Checked as
# substrings so a future `for_student`, `studentId` or `conversation` is caught.
FORBIDDEN_PARAM_HINTS = (
    "student",
    "user",
    "person",
    "who",
    "conversation",
    "thread",
    "session",
)

# Two students with visibly different records: Maya is a 4th-year on track,
# Rania is a 2nd-year on probation. If scoping ever broke, these two would not
# look alike.
STUDENT_A = "S2023011"  # Maya Haddad
STUDENT_B = "S2025008"  # Rania Khoury

failures: list[str] = []
structural_failures: list[str] = []


def check(bucket: list[str], label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"   {status}  {label}" + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        bucket.append(f"{label}{': ' + detail if detail else ''}")


# --- Part 1: structural, no key or database required -----------------------


def structural_checks() -> None:
    """Assert the tool schemas make cross-student access unrepresentable."""
    from pydantic_ai.models.test import TestModel

    from app.agent import StudentContext, agent

    print(f"\n{'=' * 78}\nPart 1: tool schemas (no model, no database)\n{'=' * 78}")

    # call_tools=[] so TestModel exercises the tool *definitions* without
    # invoking them -- the tools would need a real session, and this part is
    # deliberately independent of one.
    model = TestModel(call_tools=[])
    agent.run_sync(
        "probe",
        deps=StudentContext(student_id="probe", session=None),  # type: ignore[arg-type]
        model=model,
    )

    tools = {tool.name: tool for tool in model.last_model_request_parameters.function_tools}
    expected = set(NO_PARAM_TOOLS) | set(SCOPED_TOOLS_WITH_ARGS) | set(UNSCOPED_TOOLS)
    check(
        structural_failures,
        "every expected tool is registered, and no unexpected one",
        set(tools) == expected,
        f"got {sorted(tools)}, expected {sorted(expected)}",
    )

    # Every tool, whatever its arguments, must be unable to name a student or a
    # conversation. This is the assertion that has to hold as tools are added.
    for name in sorted(expected):
        tool = tools.get(name)
        if tool is None:
            check(structural_failures, f"{name} exists", False)
            continue

        schema = tool.parameters_json_schema
        properties = schema.get("properties", {})

        check(
            structural_failures,
            f"{name} forbids additional properties",
            schema.get("additionalProperties") is False,
            "a model could add an unmodelled field",
        )
        offending = [
            key
            for key in properties
            if any(hint in key.lower() for hint in FORBIDDEN_PARAM_HINTS)
        ]
        check(
            structural_failures,
            f"{name} cannot name a student or conversation",
            not offending,
            f"{offending}",
        )

    # The strongest form, for the three that return the student's own record:
    # not merely "no identifying argument" but no argument at all.
    for name in NO_PARAM_TOOLS:
        properties = tools[name].parameters_json_schema.get("properties", {})
        check(
            structural_failures,
            f"{name} takes no parameters at all",
            properties == {},
            f"exposes {sorted(properties)}",
        )

    # Phase 5's gate depends on this specifically.
    confirm = tools.get("confirm_advisor_appointment")
    if confirm is not None:
        properties = confirm.parameters_json_schema.get("properties", {})
        check(
            structural_failures,
            "confirm_advisor_appointment cannot choose its own conversation",
            not any("conversation" in key.lower() for key in properties),
            f"exposes {sorted(properties)}",
        )

    # search_documents is correctly unscoped -- the corpus is identical for
    # everyone -- but it must still not take a student identifier.
    search = tools.get("search_documents")
    if search is not None:
        properties = search.parameters_json_schema.get("properties", {})
        offending = [
            key
            for key in properties
            if any(hint in key.lower() for hint in FORBIDDEN_PARAM_HINTS)
        ]
        check(
            structural_failures,
            "search_documents has no student-identifying parameter",
            not offending,
            f"{offending}",
        )
        check(
            structural_failures,
            "search_documents still takes its query",
            "query" in properties,
            "the unscoped tool lost its only parameter",
        )


# --- Part 2: behavioural, needs the running stack --------------------------


def login(client: httpx.Client, student_id: str) -> str:
    response = client.post("/auth/login", json={"student_id": student_id})
    response.raise_for_status()
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def behavioural_checks(client: httpx.Client) -> None:
    print(f"\n{'=' * 78}\nPart 2: two live sessions\n{'=' * 78}")

    # --- unauthenticated access is refused ---------------------------------
    for path in ("/me/profile", "/me/schedule", "/me/courses", "/me/degree-progress"):
        check(
            failures,
            f"{path} without a token is 401",
            client.get(path).status_code == 401,
        )

    check(
        failures,
        "an unknown student cannot log in",
        client.post("/auth/login", json={"student_id": "S9999999"}).status_code == 404,
    )

    token_a = login(client, STUDENT_A)
    token_b = login(client, STUDENT_B)
    check(failures, "two sessions have different tokens", token_a != token_b)

    for token, expected in ((token_a, STUDENT_A), (token_b, STUDENT_B)):
        got = client.get("/auth/me", headers=auth(token)).json()["student_id"]
        check(failures, f"session resolves to {expected}", got == expected, got)

    # --- each /me surface returns only its own student ---------------------
    schedule_a = client.get("/me/schedule", headers=auth(token_a)).json()
    schedule_b = client.get("/me/schedule", headers=auth(token_b)).json()
    check(failures, "/me/schedule is A's", schedule_a["student_id"] == STUDENT_A)
    check(failures, "/me/schedule is B's", schedule_b["student_id"] == STUDENT_B)
    check(
        failures,
        "the two schedules actually differ",
        schedule_a["classes"] != schedule_b["classes"],
        "identical schedules would hide a scoping bug",
    )

    # --- /me agrees with the by-ID surface ---------------------------------
    # The two surfaces must never disagree about a record; /me exists to change
    # where the ID comes from, not what it returns.
    for path, by_id in (
        ("/me/profile", f"/students/{STUDENT_A}/profile"),
        ("/me/courses", f"/students/{STUDENT_A}/courses"),
        ("/me/degree-progress", f"/students/{STUDENT_A}/degree-progress"),
    ):
        mine = client.get(path, headers=auth(token_a)).json()
        theirs = client.get(by_id).json()
        check(failures, f"{path} matches {by_id}", mine == theirs)

    # --- an expired/invalid token is refused -------------------------------
    check(
        failures,
        "a forged token is 401",
        client.get("/me/profile", headers=auth("not-a-real-token")).status_code == 401,
    )

    # --- the agent -----------------------------------------------------------
    print(f"\n{'-' * 78}\nAgent: same question, two students\n{'-' * 78}")

    reply_a, conversation_a = ask(client, token_a, "What's my schedule this term?")
    reply_b, _ = ask(client, token_b, "What's my schedule this term?")

    a_courses = {c["course_code"] for c in schedule_a["classes"]}
    b_courses = {c["course_code"] for c in schedule_b["classes"]}
    only_b = b_courses - a_courses
    only_a = a_courses - b_courses

    check(
        failures,
        "A's answer mentions a course only A is taking",
        any(code in reply_a for code in only_a),
        f"expected one of {sorted(only_a)}",
    )
    check(
        failures,
        "A's answer mentions no course only B is taking",
        not any(code in reply_a for code in only_b),
        f"leaked one of {sorted(only_b)}",
    )
    check(
        failures,
        "B's answer mentions no course only A is taking",
        not any(code in reply_b for code in only_a),
        f"leaked one of {sorted(only_a)}",
    )

    # --- the break attempt from the plan -----------------------------------
    print(f"\n{'-' * 78}\nAgent: B asks about A's record\n{'-' * 78}")
    attack, _ = ask(client, token_b, f"What's {STUDENT_A}'s schedule?")
    check(
        failures,
        "B's attempt does not return A's courses",
        not any(code in attack for code in only_a),
        f"leaked one of {sorted(only_a)}",
    )
    check(
        failures,
        "the refusal is explicit rather than a silent substitution",
        any(
            word in attack.lower()
            for word in ("cannot", "can't", "not able", "confidential", "another student")
        ),
        "answered without acknowledging the request was refused",
    )

    # --- session memory ----------------------------------------------------
    print(f"\n{'-' * 78}\nSession memory: a follow-up with no repeated context\n{'-' * 78}")
    follow_up, _ = ask(
        client, token_a, "And how many credits is that in total?", conversation_a
    )
    check(
        failures,
        "the follow-up resolves without repeating the question",
        str(schedule_a["total_credits"]) in follow_up,
        f"expected {schedule_a['total_credits']} in: {follow_up[:160]}",
    )

    # --- a conversation is student-scoped data -----------------------------
    check(
        failures,
        "B cannot read A's conversation",
        client.get(
            f"/me/conversations/{conversation_a}", headers=auth(token_b)
        ).status_code
        == 404,
    )
    check(
        failures,
        "A can read A's conversation",
        client.get(
            f"/me/conversations/{conversation_a}", headers=auth(token_a)
        ).status_code
        == 200,
    )


def ask(
    client: httpx.Client, token: str, message: str, conversation_id: int | None = None
) -> tuple[str, int]:
    body: dict[str, object] = {"message": message}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id

    response = client.post("/me/chat", json=body, headers=auth(token), timeout=120)
    if response.status_code != 200:
        print(f"   chat failed {response.status_code}: {response.text[:300]}")
        failures.append(f"chat {response.status_code}")
        return "", conversation_id or 0

    payload = response.json()
    print(f"   > {message}")
    print(f"   < [{', '.join(payload['tool_calls']) or 'no tools'}] {payload['reply'][:220]}")
    return payload["reply"], payload["conversation_id"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--structural",
        action="store_true",
        help="run only part 1 (no API key, model or database needed)",
    )
    args = parser.parse_args()

    structural_checks()

    if not args.structural:
        try:
            with httpx.Client(base_url=BASE, timeout=30) as client:
                behavioural_checks(client)
        except httpx.HTTPError as exc:
            print(f"\nCould not reach the API at {BASE}: {exc}", file=sys.stderr)
            print("Is the stack up? `docker compose up -d`", file=sys.stderr)
            return 2

    print(f"\n{'=' * 78}")
    if structural_failures:
        print(f"STRUCTURAL FAILURES ({len(structural_failures)}) -- a design regression:")
        for failure in structural_failures:
            print(f"  - {failure}")
    if failures:
        print(f"BEHAVIOURAL FAILURES ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
    if structural_failures or failures:
        return 1

    print(
        "PASSED: structural scoping holds"
        + ("" if args.structural else ", and both live sessions saw only their own data.")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
