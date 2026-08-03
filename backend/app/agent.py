"""The PydanticAI agent and its tools.

PROJECT_PLAN Phase 4 states the real goal plainly: the point of this step is not
the tools, it is **proving the authenticated student ID flows from the HTTP
request, into the agent's dependency object, into the tool call, without the
model ever being asked to supply or confirm it.**

That is enforced by shape, not by instruction:

- `StudentContext` carries the ID, and it is built in the route handler from
  `auth.current_student` -- the only producer of an authenticated ID in the app.
- **No scoped tool takes a student identifier as a parameter.** `get_my_schedule`
  has no arguments at all. Because the parameter does not exist, it is absent
  from the JSON schema the model sees, so there is no field for it to fill in,
  guess at, or be talked into by a user. A prompt injection cannot supply an
  argument that the tool signature does not have.
- Every scoped tool reads `ctx.deps.student_id` and passes it to the same
  `records`/`academics` functions Phase 2 built, which filter on it in SQL.

The system prompt also tells the model to refuse cross-student questions. That is
a *courtesy*, not the control: it produces a good refusal message. If the prompt
were removed entirely, the model still could not reach another student's data,
because there is no argument through which to ask.

`search_documents` is the one unscoped tool, correctly -- the corpus is
institutional policy, identical for every student (CLAUDE.md section 6).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic_ai import Agent, RunContext
from sqlalchemy.orm import Session

from app import appointments
from app.academics import (
    get_academic_summary,
    get_degree_progress,
    is_graduation_credit_complete,
)
from app.config import get_settings
from app.eligibility import check_eligibility
from app.records import get_course_history, get_profile, get_schedule
from app.retrieval import search

# Note on formatting: `academics.format_progress_table` and `format_summary`
# exist, and are deliberately *not* reused here. They render fixed-width ASCII
# with abbreviated headers ('raw', 'crs', 'prog') for reading in a terminal
# during verification. A model reads better from explicit labelled prose, and
# an abbreviation it has to guess at is an invitation to guess wrong.


@dataclass
class StudentContext:
    """What every tool call is scoped to.

    Built in the route handler from the authenticated session and nowhere else.
    The model never sees this object; it sees only the tools' parameters, which
    is why none of them has a student identifier.
    """

    student_id: str
    session: Session
    # Which thread this run belongs to. Carried here rather than passed as a tool
    # argument for the same reason as `student_id`: the appointment confirmation
    # gate reads this conversation's stored history to prove a proposal came
    # first, and a model-supplied id would let it point the check at a
    # conversation where a proposal did happen.
    conversation_id: int | None = None


agent = Agent(
    # No model here on purpose. The admin configures it in `assistant_settings`,
    # so it is passed per-run in `chat.py` -- binding one at construction time
    # would make the setting cosmetic.
    deps_type=StudentContext,
    # `defer_model_check` because the model name is only known per-run; without
    # it, constructing the agent at import time would try to validate a model
    # that has not been chosen yet.
    defer_model_check=True,
)


@agent.tool
async def search_documents(ctx: RunContext[StudentContext], query: str) -> str:
    """Search the Course Catalogue and Student Handbook for a passage.

    Use this for anything about policy, deadlines, fees, grading, academic
    standing, graduation requirements, course descriptions or prerequisites, and
    for finding which office handles an enquiry. Search before answering; do not
    answer such questions from memory.

    Args:
        query: A natural-language question, in the student's own words.
    """
    hits = search(ctx.deps.session, query, top_k=get_settings().retrieval_top_k)
    if not hits:
        return (
            "No matching passage found in the Course Catalogue or Student "
            "Handbook. Tell the student you could not find this in the "
            "University's documents rather than answering from memory."
        )

    # Rendered as text rather than JSON: the citation has to arrive attached to
    # the passage it belongs to, so the model cannot pair the right quote with
    # the wrong source.
    return "\n\n".join(
        f"[Source: {hit.citation()}]\n{hit.content}" for hit in hits
    )


@agent.tool
async def get_my_schedule(ctx: RunContext[StudentContext]) -> str:
    """The current term's classes for the student you are speaking to.

    Days, times, rooms and instructors. Takes no arguments: it always returns the
    logged-in student's own schedule and cannot return anyone else's.
    """
    term = get_settings().current_term
    classes = get_schedule(ctx.deps.session, ctx.deps.student_id, term)
    if not classes:
        return f"Not registered for any classes in {term}."

    lines = [
        f"{c['course_code']} {c['title']} ({c['credits']} cr) - "
        f"{c['days']} {c['start_time']:%H:%M}-{c['end_time']:%H:%M}, "
        f"room {c['room']}, {c['instructor']}"
        for c in classes
    ]
    total = sum(c["credits"] for c in classes)
    return f"{term} schedule ({total} credits):\n" + "\n".join(lines)


@agent.tool
async def get_my_courses(ctx: RunContext[StudentContext]) -> str:
    """The full academic history of the student you are speaking to.

    Every course taken or in progress, with grades, plus cumulative GPA and
    credits. Takes no arguments: it always returns the logged-in student's own
    record. Use this rather than calculating anything yourself.
    """
    session, student_id = ctx.deps.session, ctx.deps.student_id
    summary = get_academic_summary(session, student_id)
    courses = get_course_history(session, student_id)

    gpa = summary["gpa"]
    header = (
        f"Cumulative GPA: {gpa if gpa is not None else 'none yet (no graded courses)'}"
        f" | credits earned: {summary['credits_earned']}"
        f" | credits in progress: {summary['credits_in_progress']}"
    )
    if not courses:
        return f"{header}\nNo courses on record yet."

    lines = [
        f"{c['term_code']} {c['course_code']} {c['title']} ({c['credits']} cr) - "
        f"{c['grade'] if c['grade'] else c['status']}"
        for c in courses
    ]
    return f"{header}\n" + "\n".join(lines)


@agent.tool
async def get_my_degree_progress(ctx: RunContext[StudentContext]) -> str:
    """Degree progress by requirement category, for the student you are speaking to.

    Takes no arguments: always the logged-in student's own progress.

    Report these numbers as given. In particular, do not add the categories up
    and compare the total to 72 as though that were the graduation test --
    surplus credits in one category never offset a shortfall in another, so a
    student can be well past half the credits and still several terms away.
    """
    session, student_id = ctx.deps.session, ctx.deps.student_id
    profile = get_profile(session, student_id)
    if profile is None:
        return "No record found for the current student."

    categories = get_degree_progress(session, student_id)
    summary = get_academic_summary(session, student_id)

    lines = [
        f"{c['category_name']}: {c['credits_applied']}/{c['credits_required']} credits"
        + (
            f" - SATISFIED"
            if c["is_satisfied"]
            else f" - {c['credits_remaining']} still needed"
        )
        + (
            f" (rule: any {c['courses_required']} of {c['courses_offered']} courses)"
            if c["selection_rule"] == "ANY_N"
            else f" (rule: all {c['courses_offered']} courses required)"
        )
        + (
            f" [{c['credits_in_progress']} cr in progress]"
            if c["credits_in_progress"]
            else ""
        )
        for c in categories
    ]
    unsatisfied = [c["category_name"] for c in categories if not c["is_satisfied"]]
    # The canonical test, shared with the endpoint, rather than a second
    # implementation of "are we done?" that could drift from it.
    complete = is_graduation_credit_complete(categories)

    return (
        f"Programme: {profile['program_name']} "
        f"({summary['credits_earned']}/{profile['total_credits_required']} credits earned)\n"
        + "\n".join(lines)
        + "\n"
        + (
            "Every requirement category is satisfied."
            if complete
            else f"Categories not yet satisfied: {', '.join(unsatisfied)}."
        )
    )


@agent.tool
async def check_course_eligibility(
    ctx: RunContext[StudentContext], course_code: str
) -> str:
    """Whether the student you are speaking to may register for a course, and why.

    This is the tool for "Am I allowed to take X?", "Can I register for X?" and
    "What do I need before X?". It already combines the course's prerequisites,
    the C- or above rule, how many attempts the student has used, whether the
    course runs this term, and the credit-load and probation caps. Two students
    get different answers for the same course; that is correct.

    Report the reasons it gives. Do not add prerequisites of your own, and do not
    overrule a refusal because the student's transcript looks fine to you -- the
    caps are the part that is easy to miss.

    Args:
        course_code: The course code as printed in the Catalogue, e.g. "MECH 310".
    """
    result = check_eligibility(ctx.deps.session, ctx.deps.student_id, course_code)
    if result is None:
        # Not "you are ineligible": a student asking about a course that does not
        # exist has made a typo, and answering the question they did not ask
        # would be a confident wrong answer.
        return (
            f"There is no course {course_code!r} in the Catalogue. Check the code "
            "with the student; do not guess at what they meant."
        )

    lines = [
        f"{result['course_code']} {result['title']} ({result['credits']} credits), "
        f"term {result['term_code']}",
        f"Eligible: {'yes' if result['eligible'] else 'NO'}",
    ]
    if result["requires_advisor_approval"]:
        lines.append(
            "Permitted only with the written approval of the academic advisor."
        )

    if result["prerequisites"]:
        lines.append("Prerequisites:")
        lines.extend(
            f"  - {p['course_code']} {p['title']}: "
            + (
                f"met (grade {p['best_grade']})"
                if p["satisfied"]
                else (
                    "in progress, not yet complete"
                    if p["currently_taking"]
                    else f"NOT met (best grade so far: {p['best_grade'] or 'never taken'})"
                )
            )
            for p in result["prerequisites"]
        )
    else:
        lines.append("Prerequisites: none.")

    lines.append(
        f"Standing: {result['academic_status']}. "
        f"Registered {result['registered_credits']} credits, "
        f"cap {result['credit_cap']}."
    )
    lines.append("Findings:")
    lines.extend(
        f"  - [{'ok' if reason['satisfied'] else 'BLOCKER'}] "
        f"{reason['rule']}: {reason['detail']}"
        for reason in result["reasons"]
    )
    if result["notes"]:
        lines.extend(f"  note: {note}" for note in result["notes"])

    return "\n".join(lines)


@agent.tool
async def request_advisor_appointment(
    ctx: RunContext[StudentContext], reason: str
) -> str:
    """Propose a meeting with the student's academic advisor. **Books nothing.**

    Returns a suggested advisor, date and time. Present it to the student and ask
    whether they want it booked. Do NOT call `confirm_advisor_appointment` in the
    same reply -- the student has not answered yet, and the confirmation will be
    refused if you try.

    Use this when a question genuinely needs a human: probation planning, a
    prerequisite waiver, a study plan, or anything the documents do not settle.

    Args:
        reason: Why the student wants to see their advisor, in one short phrase.
    """
    try:
        proposal = appointments.propose(ctx.deps.session, ctx.deps.student_id, reason)
    except LookupError:
        return "No record found for the current student."

    return (
        f"PROPOSAL ONLY - nothing has been booked.\n"
        f"{proposal.describe()}\n"
        f"Confirmed time to pass back if the student agrees: "
        f"{proposal.proposed_time.isoformat()}\n"
        "Ask the student to confirm before booking. If they want a different "
        "time, say that the Advising Centre (advising@eurisko.edu) can arrange "
        "one; do not invent alternative slots."
    )


@agent.tool
async def confirm_advisor_appointment(
    ctx: RunContext[StudentContext], proposed_time: str, reason: str
) -> str:
    """Book an appointment the student has just explicitly agreed to.

    Only call this after you proposed a time in an earlier reply AND the student
    answered yes. It will refuse otherwise -- that refusal is a safety check, not
    an error to work around, so do not retry it or invent a different time.

    Args:
        proposed_time: The exact ISO timestamp from the proposal.
        reason: The reason from the proposal.
    """
    if ctx.deps.conversation_id is None:
        return "Cannot book an appointment outside a conversation."

    try:
        when = datetime.fromisoformat(proposed_time)
    except ValueError:
        return (
            f"{proposed_time!r} is not a valid timestamp. Use the exact value "
            "from the proposal."
        )

    try:
        appointment = appointments.confirm(
            ctx.deps.session,
            ctx.deps.student_id,
            conversation_id=ctx.deps.conversation_id,
            proposed_time=when,
            reason=reason,
        )
    except appointments.NotProposedFirst as exc:
        return (
            f"REFUSED: {exc} Nothing has been booked. Propose a time, then wait "
            "for the student's answer."
        )
    except appointments.NoSuchSlot as exc:
        return f"REFUSED: {exc} Nothing has been booked."
    except LookupError:
        return "No record found for the current student."

    return (
        f"Booked: {appointment.advisor_name} on "
        f"{appointment.proposed_time:%A %d %B %Y at %H:%M}. "
        f"Reason: {appointment.reason}."
    )
