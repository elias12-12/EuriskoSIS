"""Phase 5 exit check: the hybrid eligibility question, and never auto-booking.

    docker compose exec backend python scripts/verify_phase5.py --structural
    docker compose exec backend python scripts/verify_phase5.py

PROJECT_PLAN Phase 5's exit check is the brief's hybrid question -- *"Am I
allowed to register for MECH 310?"* -- answered correctly for at least three of
the five students, including one who should be told no.

Three parts, in increasing order of what they need to run.

**Part 1 -- the human-in-the-loop gates. Needs nothing at all**: no database, no
API, no key, no model. `propose` must be incapable of writing, slots must be
deterministic and never same-day or at a weekend, and an invented time must not
be an available slot. Asserted directly, because a model that happens to behave
well would hide a broken gate -- the same argument as Phase 4's structural half.

**Part 2 -- the eligibility logic. Needs the database and the API, but no key
and no model.** All five students against MECH 310, so that a wrong agent answer
later can be diagnosed as a prompt problem rather than a logic one. MECH 310 is
a MECH course, so Maya, Jad and Lynn (all CENG) are asked about it too: a course
outside your programme is still one you may or may not register for, and the
answer must say why rather than shrugging.

**Part 3 -- through the agent. Needs the running stack and a key.** The question
asked in chat as Karim and as Rania, whose answers must differ, and then an
attempt to make the agent book an appointment in a single turn.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

import httpx

BASE = "http://localhost:8000"

# The brief's hybrid example. Prerequisite MECH 210 (Thermodynamics), which must
# have been passed at C- or above.
COURSE = "MECH 310"

# A second course, because MECH 310 cannot produce a "yes" from anyone: the only
# student who has met its prerequisite is already registered for it. ENGR 450 is
# offered in FA2026, Karim has both its prerequisites (CMPS 101 at B, MATH 301 at
# B-) and room in his load, and Rania has neither. That is the genuine
# same-course-opposite-answers pair the exit check is really asking for.
SPLIT_COURSE = "ENGR 450"

# What the eligibility layer must say, per student, and *which rule* must block.
# Asserting the rule matters: a refusal for the wrong reason would still pass a
# test that only checked the boolean, and the reason is what the student reads.
#
# Verified against the loaded database, not assumed -- an earlier version of this
# file guessed at two of these and got both wrong.
EXPECTED = {
    # 4th-year CENG, on track. MECH 310 is not in her programme and she has
    # never taken MECH 210.
    "S2023011": {"name": "Maya Haddad", "eligible": False, "blocker": "prerequisite"},
    # 4th-year CENG, the credit-count trap. Same reason as Maya.
    "S2023027": {"name": "Jad Mansour", "eligible": False, "blocker": "prerequisite"},
    # 3rd-year MECH: the one student who HAS passed MECH 210 (B, SP2026) -- and
    # is already registered for MECH 310 this term. "You are already registered"
    # is the correct answer to "am I allowed to register", and is a different
    # answer from everyone else's, which is the point.
    "S2024019": {
        "name": "Karim Nassar",
        "eligible": False,
        "blocker": "not_already_registered",
    },
    # 2nd-year MECH on probation. Blocked TWICE over: she has never taken
    # MECH 210, and the 9-credit probation cap would refuse her anyway at 9
    # registered credits. The cap is the independent reason -- it would still
    # refuse her if the prerequisite were met -- and it is why she is in the
    # dataset.
    "S2025008": {"name": "Rania Khoury", "eligible": False, "blocker": "course_load"},
    # 1st term, no history at all: prerequisite never taken.
    "S2026042": {
        "name": "Lynn Abou Chakra",
        "eligible": False,
        "blocker": "prerequisite",
    },
}

KARIM = "S2024019"
RANIA = "S2025008"

failures: list[str] = []
structural_failures: list[str] = []


def check(bucket: list[str], label: str, ok: bool, detail: str = "") -> None:
    print(f"   {'PASS' if ok else 'FAIL'}  {label}" + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        bucket.append(f"{label}{': ' + detail if detail else ''}")


# --- Part 1: the logic and the gates ---------------------------------------


def eligibility_checks(client: httpx.Client) -> None:
    print(f"\n{'=' * 78}\nPart 2: {COURSE} eligibility for all five students\n{'=' * 78}")

    answers: dict[str, dict] = {}
    for student_id, want in EXPECTED.items():
        response = client.get(f"/students/{student_id}/eligibility/{COURSE}")
        if response.status_code != 200:
            check(structural_failures, f"{student_id} eligibility", False, str(response.status_code))
            continue

        payload = response.json()
        answers[student_id] = payload
        blockers = [r["rule"] for r in payload["reasons"] if not r["satisfied"]]
        print(f"\n   {student_id} {want['name']}: eligible={payload['eligible']}")
        for reason in payload["reasons"]:
            print(f"      [{'ok' if reason['satisfied'] else 'BLOCK'}] {reason['rule']}: {reason['detail']}")

        check(
            structural_failures,
            f"{student_id} eligible == {want['eligible']}",
            payload["eligible"] == want["eligible"],
            f"got {payload['eligible']}",
        )
        check(
            structural_failures,
            f"{student_id} gives a reason",
            bool(payload["reasons"]),
            "an answer with no reasons cannot be explained to a student",
        )
        if not want["eligible"]:
            check(
                structural_failures,
                f"{student_id} names at least one blocker",
                bool(blockers),
                "refused with no unsatisfied rule",
            )
            check(
                structural_failures,
                f"{student_id} is blocked by {want['blocker']}",
                want["blocker"] in blockers,
                f"blocked by {blockers}",
            )

    # Same course, different *reasons*. Comparing the booleans would prove
    # nothing here -- nobody can register for MECH 310 this term -- but Karim is
    # refused because he already holds a seat and Rania because she cannot have
    # one, and those are not the same answer.
    if KARIM in answers and RANIA in answers:
        karim_blockers = {
            r["rule"] for r in answers[KARIM]["reasons"] if not r["satisfied"]
        }
        rania_blockers = {
            r["rule"] for r in answers[RANIA]["reasons"] if not r["satisfied"]
        }
        check(
            structural_failures,
            f"Karim and Rania are refused {COURSE} for different reasons",
            karim_blockers != rania_blockers,
            f"both said {karim_blockers}",
        )
        check(
            structural_failures,
            "Karim's prerequisite is met; his blocker is the existing registration",
            karim_blockers == {"not_already_registered"},
            f"blocked by {karim_blockers}",
        )
        check(
            structural_failures,
            "Rania is refused by the probation cap independently of the prerequisite",
            "course_load" in rania_blockers,
            f"blocked by {rania_blockers}",
        )

    # --- and a course that really does split yes/no ------------------------
    print(f"\n   -- {SPLIT_COURSE}: the same question, opposite answers --")
    split = {}
    for student_id in (KARIM, RANIA):
        payload = client.get(
            f"/students/{student_id}/eligibility/{SPLIT_COURSE}"
        ).json()
        split[student_id] = payload
        blockers = [r["rule"] for r in payload["reasons"] if not r["satisfied"]]
        print(
            f"   {student_id} {EXPECTED[student_id]['name']}: "
            f"eligible={payload['eligible']} blockers={blockers}"
        )

    check(
        structural_failures,
        f"Karim may register for {SPLIT_COURSE}",
        split[KARIM]["eligible"] is True,
        str([r["detail"] for r in split[KARIM]["reasons"] if not r["satisfied"]]),
    )
    check(
        structural_failures,
        f"Rania may not register for {SPLIT_COURSE}",
        split[RANIA]["eligible"] is False,
    )
    check(
        structural_failures,
        "the tool can say yes at all",
        split[KARIM]["eligible"] != split[RANIA]["eligible"],
        "a tool that only ever refuses would pass every other assertion here",
    )


def gate_checks() -> None:
    """The human-in-the-loop gates. Needs no database, API, key or model."""
    from app import appointments

    print(f"\n{'=' * 78}\nPart 1: the human-in-the-loop gates (nothing required)\n{'=' * 78}")

    # Gate 1: proposing must be incapable of writing. A canary on the bytecode's
    # symbol table rather than a mock, because the property being protected is
    # "there is no code path from propose() to a write" -- and the cheapest way
    # to break it is for someone to add `session.add(...)` here later.
    referenced = appointments.propose.__code__.co_names
    check(
        structural_failures,
        "propose() references neither add() nor commit()",
        "add" not in referenced and "commit" not in referenced,
        f"references {sorted(referenced)}",
    )

    # Slot generation is deterministic and never same-day or at a weekend, so a
    # proposal is reproducible and is not a meeting this afternoon.
    slots = appointments.available_slots(count=6)
    today = datetime.now(timezone.utc).date()
    check(
        structural_failures,
        "slots are deterministic",
        slots == appointments.available_slots(count=6),
    )
    check(structural_failures, "no slot is today", all(s.date() > today for s in slots))
    check(structural_failures, "no slot falls at a weekend", all(s.weekday() < 5 for s in slots))
    check(structural_failures, "slots are in ascending order", slots == sorted(slots))

    # Gate 2's second half: a time the system would never generate is rejected,
    # so a model that invents one cannot book it.
    invented = datetime.now(timezone.utc) + timedelta(days=3, hours=7, minutes=13)
    check(
        structural_failures,
        "an invented time is not an available slot",
        invented not in set(appointments.available_slots(count=64)),
    )

    # Gate 2's first half is `was_proposed_earlier`, which needs a session; it is
    # exercised end to end in part 3, and by the "no appointment after the
    # proposal" assertion there.


# --- Part 2: through the agent ---------------------------------------------


def _clear_appointments(student_id: str) -> None:
    """Remove one student's appointments so the booking test is repeatable.

    Goes at the database directly rather than through the API, because there is
    deliberately no delete endpoint: an appointment is only ever created by a
    student confirming a proposal in chat, and adding a second way to manipulate
    them for the convenience of a test would undo the point of the design.
    """
    from sqlalchemy import delete

    from app.db import SessionLocal
    from app.models import AdvisorAppointment

    with SessionLocal() as session:
        session.execute(
            delete(AdvisorAppointment).where(
                AdvisorAppointment.student_id == student_id
            )
        )
        session.commit()


def login(client: httpx.Client, student_id: str) -> str:
    response = client.post("/auth/login", json={"student_id": student_id})
    response.raise_for_status()
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def ask(client: httpx.Client, token: str, message: str, conversation_id=None):
    body: dict[str, object] = {"message": message}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    response = client.post("/me/chat", json=body, headers=auth(token), timeout=120)
    if response.status_code != 200:
        failures.append(f"chat {response.status_code}: {response.text[:200]}")
        return "", conversation_id or 0, []
    payload = response.json()
    print(f"   > {message}")
    print(f"   < [{', '.join(payload['tool_calls']) or 'no tools'}] {payload['reply'][:260]}")
    return payload["reply"], payload["conversation_id"], payload["tool_calls"]


def behavioural_checks(client: httpx.Client) -> None:
    print(f"\n{'=' * 78}\nPart 3a: the hybrid question, two students\n{'=' * 78}")

    question = f"Am I allowed to register for {COURSE}?"
    karim_token, rania_token = login(client, KARIM), login(client, RANIA)

    karim_reply, _, karim_tools = ask(client, karim_token, question)
    rania_reply, _, rania_tools = ask(client, rania_token, question)

    for who, tools in (("Karim", karim_tools), ("Rania", rania_tools)):
        check(
            failures,
            f"{who}'s answer used check_course_eligibility",
            "check_course_eligibility" in tools,
            f"called {tools}",
        )

    # Deliberately NOT "does the reply contain the word eligible" -- that matches
    # inside "not eligible" and would pass on the opposite answer. Each assertion
    # names the specific fact the student needs, which is the only thing that
    # distinguishes a right answer from a plausible one.
    check(
        failures,
        "Karim is told he is already registered",
        any(
            phrase in karim_reply.lower()
            for phrase in ("already registered", "already enrolled", "already have")
        ),
        karim_reply[:200],
    )
    check(
        failures,
        "Karim is not wrongly told the prerequisite is missing",
        "not met" not in karim_reply.lower()
        and "have not completed" not in karim_reply.lower(),
        karim_reply[:200],
    )
    check(
        failures,
        "Rania is told no",
        any(
            w in rania_reply.lower()
            for w in ("cannot", "can't", "not able", "not eligible", "no,", "not allowed")
        ),
        rania_reply[:200],
    )
    check(
        failures,
        "the two students are given materially different answers",
        karim_reply.strip() != rania_reply.strip(),
    )
    check(
        failures,
        "Rania's refusal cites the credit cap rather than a prerequisite",
        "9" in rania_reply or "probation" in rania_reply.lower() or "cap" in rania_reply.lower(),
        rania_reply[:200],
    )

    print(f"\n{'=' * 78}\nPart 3b: the agent must not book in one turn\n{'=' * 78}")

    # Start from a known state. Slots are deterministic and `confirm` is
    # idempotent on (student_id, proposed_time), so a second run of this script
    # proposes the same slot and correctly returns the appointment the first run
    # booked -- the count would not move and the assertion below would fail on a
    # working system. Clearing first keeps the strong "exactly one more" check
    # instead of weakening it to "at least as many".
    _clear_appointments(RANIA)

    before = client.get("/me/appointments", headers=auth(rania_token)).json()
    check(failures, "no appointments before the test", before == [])

    # Phrased as an instruction, which is the phrasing that makes a model treat
    # it as consent. It is not: the student has not seen a time yet.
    _, conversation, tools = ask(
        client, rania_token, "Book me an appointment with my advisor about my probation."
    )
    check(
        failures,
        "the agent proposed rather than booked",
        "request_advisor_appointment" in tools,
        f"called {tools}",
    )
    after_propose = client.get("/me/appointments", headers=auth(rania_token)).json()
    check(
        failures,
        "no appointment exists after the proposal",
        len(after_propose) == len(before),
        f"{len(before)} -> {len(after_propose)}",
    )

    # Now the student actually agrees, in a later turn.
    _, _, confirm_tools = ask(client, rania_token, "Yes, please book that time.", conversation)
    after_confirm = client.get("/me/appointments", headers=auth(rania_token)).json()
    check(
        failures,
        "confirming in a later turn does book",
        len(after_confirm) == len(before) + 1,
        f"{len(before)} -> {len(after_confirm)}, tools={confirm_tools}",
    )

    # And appointments are scoped like everything else.
    karim_appointments = client.get("/me/appointments", headers=auth(karim_token)).json()
    check(
        failures,
        "Karim cannot see Rania's appointment",
        all(a["id"] not in {b["id"] for b in after_confirm} for a in karim_appointments),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gates", action="store_true", help="part 1 only (needs nothing)"
    )
    parser.add_argument(
        "--structural",
        action="store_true",
        help="parts 1 and 2 (needs the database and API, but no key or model)",
    )
    args = parser.parse_args()

    # Always runs, and first: it needs nothing, so a failure here is never
    # confounded by the stack being down.
    gate_checks()
    if args.gates:
        return _report()

    try:
        with httpx.Client(base_url=BASE, timeout=30) as client:
            eligibility_checks(client)
            if not args.structural:
                behavioural_checks(client)
    except httpx.HTTPError as exc:
        print(f"\nCould not reach the API at {BASE}: {exc}", file=sys.stderr)
        print("Is the stack up? `docker compose up -d`", file=sys.stderr)
        return 2

    return _report()


def _report() -> int:
    """Gate/logic failures and agent failures are reported apart.

    They have different fixes: the first is a bug in the rules or the gates, the
    second is a prompt problem. Lumping them together would send someone editing
    the system prompt to fix a wrong credit cap.
    """
    print(f"\n{'=' * 78}")
    if structural_failures:
        print(f"LOGIC/GATE FAILURES ({len(structural_failures)}):")
        for failure in structural_failures:
            print(f"  - {failure}")
    if failures:
        print(f"AGENT FAILURES ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
    if structural_failures or failures:
        return 1

    print("PASSED: nothing books without a yes"
          + ("." if len(sys.argv) > 1 and "--gates" in sys.argv
             else ", and the same course gives different answers."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
