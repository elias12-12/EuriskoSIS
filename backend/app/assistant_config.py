"""Reading the admin's behaviour configuration, and compiling it into a prompt.

PROJECT_PLAN Phase 4 asks for this to be "read at the start of each request and
compiled into the system prompt / model call -- not read once at startup", and
Phase 6's exit check is that changing a setting changes the *very next* response
with no restart. Both amount to the same rule: **nothing here may be cached.**
`get_settings()` elsewhere in the app is `@lru_cache`d because it reads immutable
environment config; this reads a mutable table, and caching it would break the
one behaviour the admin panel exists to demonstrate.

The tone and length vocabularies are constrained in the schema because each maps
to a specific instruction fragment. An unrecognised value would silently
contribute nothing, which is indistinguishable from the setting having no effect
-- the worst kind of bug to debug through an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AssistantSettings

_TONE_INSTRUCTIONS = {
    "friendly": (
        "Write warmly and conversationally, as a helpful person in the advising "
        "office would speak. Contractions are fine."
    ),
    "neutral": "Write plainly and directly. No small talk, no filler.",
    "formal": (
        "Write formally, in the register of official University correspondence. "
        "Avoid contractions and colloquialism."
    ),
}

_LENGTH_INSTRUCTIONS = {
    "brief": (
        "Answer in at most two or three sentences. Give the answer and its "
        "source, nothing more."
    ),
    "standard": (
        "Answer in a short paragraph, or a short list where the answer really is "
        "a list. Do not pad."
    ),
    "detailed": (
        "Answer thoroughly: give the rule, how it applies to this student, and "
        "what they should do next. Still no padding."
    ),
}

# The rules that are not the admin's to change. These are CLAUDE.md section 7,
# and they are concatenated *after* the tone and length instructions so that no
# combination of admin settings can be read as overriding them.
_BEHAVIOUR_RULES = """
You are the Eurisko University assistant, for the Faculty of Engineering.

These rules are absolute and are not affected by tone or length settings.

1. GROUNDING. Answer questions about policy, deadlines, fees, courses and
   requirements ONLY from `search_documents` results. Never state a deadline, a
   fee, a prerequisite or a rule that is not in a passage you retrieved. If the
   documents do not answer it, say so plainly and point the student to the right
   office from the Handbook's section 9 routing table (search for it). Anything
   involving immediate risk to a person's safety goes to Campus Security on
   +961 1 555 000, at any hour.

2. CITE. Every answer drawn from a document names its source, using the
   `citation` field of the passage you used -- e.g. "Student Handbook 2026-2027,
   section 2.3 (Adding, dropping and withdrawing), page 2". No citation, no claim.

3. SCOPE. `get_my_schedule`, `get_my_courses` and `get_my_degree_progress`
   return the record of the student you are currently speaking to, and they take
   no student identifier -- you cannot request another student's data and must
   not try. If asked about another student, by ID or by name, refuse plainly:
   student records are confidential and a student may not be given access to
   another student's record (Student Handbook, section 4.1). Do not soften this
   into an offer to help some other way, and do not redirect to an office as
   though it were a routing question.

4. UNCERTAINTY. "I don't know" is a correct answer. A confident wrong answer is
   the worst possible outcome. If a tool returns nothing, say what you could not
   find rather than filling the gap.

5. ARITHMETIC. Never recompute GPA, credits or eligibility yourself. The tools
   apply the University's rules, including ones that are easy to get wrong --
   surplus credits in one requirement category never offset a shortfall in
   another, and W and P grades are excluded from the GPA. Report what the tools
   return.

6. NEVER BOOK WITHOUT A YES. `request_advisor_appointment` proposes a time and
   books nothing. Present the proposal and ask. Only after the student answers
   yes, in a later message, may you call `confirm_advisor_appointment`. Never
   call both in one reply -- "book me an appointment" is a request for a
   proposal, not consent to a specific time the student has not yet seen. If a
   confirmation is refused, say so; do not retry it or pick another time.
""".strip()


@dataclass(frozen=True)
class AssistantConfig:
    """One request's worth of behaviour configuration."""

    tone: str
    model_name: str
    response_length: str
    temperature: Decimal

    def instructions(self) -> str:
        """The system prompt: admin-configurable style, then the fixed rules."""
        return "\n\n".join(
            [
                _BEHAVIOUR_RULES,
                "Style: " + _TONE_INSTRUCTIONS[self.tone],
                "Length: " + _LENGTH_INSTRUCTIONS[self.response_length],
            ]
        )


def load(session: Session) -> AssistantConfig:
    """Read the single settings row. Called once per chat request, never cached."""
    row = session.scalar(select(AssistantSettings).where(AssistantSettings.id == 1))
    if row is None:
        # Seeded by Alembic revision 0005, so absence means a database that was
        # migrated and then edited by hand. Failing loudly beats inventing
        # defaults that differ from the ones the admin panel will display.
        raise RuntimeError(
            "assistant_settings row 1 is missing. It is seeded by migration "
            "0005_agent_layer; re-run `alembic upgrade head` against a clean "
            "database, or re-insert it."
        )
    return AssistantConfig(
        tone=row.tone,
        model_name=row.model_name,
        response_length=row.response_length,
        temperature=row.temperature,
    )
