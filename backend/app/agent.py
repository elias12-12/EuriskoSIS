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

from pydantic_ai import Agent, RunContext
from sqlalchemy.orm import Session

from app.academics import (
    get_academic_summary,
    get_degree_progress,
    is_graduation_credit_complete,
)
from app.config import get_settings
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
