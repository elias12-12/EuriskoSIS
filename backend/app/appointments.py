"""Proposing and confirming advisor appointments.

CLAUDE.md section 6 requires `request_advisor_appointment` to return a
**proposed** appointment only -- never auto-booking, and requiring explicit user
confirmation before anything is persisted. PROJECT_PLAN Phase 5 spells out the
shape: the tool returns a proposal object and does not write to the database; the
agent presents it in chat; only an explicit follow-up from the user triggers a
second call that actually persists it.

The obvious implementation is to write both tools and tell the model in the
system prompt not to call the second one until the student agrees. That is not
good enough, for the same reason the Phase 4 scoping is not a prompt: it makes
correct behaviour a property of the model rather than of the system. A model that
calls both tools in a single turn -- which they do, when a user says "book me an
appointment with my advisor" and it reads that as consent -- would silently book
something nobody confirmed.

So confirmation is enforced structurally, by **two independent gates**:

1. `propose` is a pure function. It touches no session and returns no id. There
   is no code path from it to a database write, so it cannot book by accident.
2. `confirm` requires evidence that a proposal was made **in an earlier turn of
   the same conversation**, and it looks for that evidence in the stored message
   history rather than trusting an argument. Because the current turn's messages
   are not persisted until the run finishes, a proposal made in *this* turn is
   not yet visible -- which is exactly what makes propose-then-confirm in one
   breath fail.

Gate 2 is the interesting one: it converts "the user must have had a chance to
say no" into a database query, and `verify_phase5.py` can assert it without an
LLM in the loop.

Slot times are generated here, deterministically, and never by the model. An
appointment time is a fact about the world; a hallucinated one would be exactly
the invented-deadline failure CLAUDE.md section 7 rule 1 forbids.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AdvisorAppointment, Message, Student

# Advising hours, as a fact of the office rather than a model's invention. Not in
# the Handbook -- section 8 gives the Advising Centre's remit and contact but no
# hours -- so these are an application default, and the proposal says so rather
# than implying the Handbook specifies them.
_SLOT_HOURS = (10, 11, 14, 15)
_ADVISING_DAYS = (0, 1, 2, 3, 4)  # Monday-Friday
_EARLIEST_LEAD_DAYS = 1  # never propose today
_SEARCH_HORIZON_DAYS = 14

PROPOSAL_TOOL_NAME = "request_advisor_appointment"


@dataclass(frozen=True)
class Proposal:
    """A suggested appointment. Nothing about this has been written down."""

    student_id: str
    advisor_name: str
    proposed_time: datetime
    reason: str

    def describe(self) -> str:
        return (
            f"Proposed appointment with {self.advisor_name} on "
            f"{self.proposed_time:%A %d %B %Y at %H:%M}. Reason: {self.reason}"
        )


class NotProposedFirst(RuntimeError):
    """Confirmation attempted with no proposal in an earlier turn."""


class NoSuchSlot(ValueError):
    """The confirmed time is not one this system ever proposed."""


def available_slots(
    *, after: datetime | None = None, count: int = 3
) -> list[datetime]:
    """The next `count` advising slots, deterministically.

    Deterministic so a test can assert them and so two runs of the same request
    propose the same time. Weekends and the current day are skipped -- proposing
    a meeting for this afternoon is not a useful suggestion from a system that
    cannot see the advisor's calendar.
    """
    now = after or datetime.now(timezone.utc)
    slots: list[datetime] = []
    day: date = now.date() + timedelta(days=_EARLIEST_LEAD_DAYS)

    for offset in range(_SEARCH_HORIZON_DAYS):
        candidate_day = day + timedelta(days=offset)
        if candidate_day.weekday() not in _ADVISING_DAYS:
            continue
        for hour in _SLOT_HOURS:
            slots.append(
                datetime.combine(candidate_day, time(hour), tzinfo=timezone.utc)
            )
            if len(slots) == count:
                return slots
    return slots


def propose(
    session: Session, student_id: str, reason: str, *, slot_index: int = 0
) -> Proposal:
    """Build a proposal. **Writes nothing.**

    Reads the student's advisor name, which is the one piece of real data a
    proposal needs. Everything else is computed. The absence of a `session.add`
    or `session.commit` anywhere in this function is the first of the two gates
    described in the module docstring -- keep it that way.
    """
    advisor = session.scalar(
        select(Student.advisor_name).where(Student.student_id == student_id)
    )
    if advisor is None:
        raise LookupError(f"No student {student_id}")

    slots = available_slots(count=slot_index + 1)
    return Proposal(
        student_id=student_id,
        advisor_name=advisor,
        proposed_time=slots[slot_index],
        reason=reason.strip(),
    )


def was_proposed_earlier(session: Session, conversation_id: int) -> bool:
    """Did a proposal happen in a *previous* turn of this conversation?

    The second gate. Reads the persisted message history for a call to the
    proposal tool. Messages for the current turn are written only after the agent
    run completes, so a proposal made moments ago in this same run is invisible
    here -- which is the point: the student has not yet had the chance to answer
    it.
    """
    rows = session.scalars(
        select(Message.model_message).where(
            Message.conversation_id == conversation_id,
            Message.model_message.is_not(None),
        )
    ).all()

    for message in rows:
        for part in _parts(message):
            if (
                part.get("part_kind") == "tool-call"
                and part.get("tool_name") == PROPOSAL_TOOL_NAME
            ):
                return True
    return False


def _parts(message: object) -> list[dict]:
    """The parts of a stored PydanticAI message, defensively.

    Stored JSON, so its shape is whatever the PydanticAI version at write time
    produced. A row that does not look like a message is skipped rather than
    raising: a malformed history should not make confirming an appointment
    impossible.
    """
    if not isinstance(message, dict):
        return []
    parts = message.get("parts")
    return [part for part in parts if isinstance(part, dict)] if isinstance(parts, list) else []


def confirm(
    session: Session,
    student_id: str,
    *,
    conversation_id: int,
    proposed_time: datetime,
    reason: str,
) -> AdvisorAppointment:
    """Persist an appointment the student has explicitly agreed to.

    Raises `NotProposedFirst` if no proposal exists in an earlier turn, and
    `NoSuchSlot` if the time is not one this system generates -- so a model that
    invents a time, or confirms without proposing, fails loudly instead of
    booking.
    """
    if not was_proposed_earlier(session, conversation_id):
        raise NotProposedFirst(
            "No appointment was proposed earlier in this conversation. Propose "
            "one and let the student answer before confirming."
        )

    # The time must be one we could have proposed. Compared against a generous
    # horizon rather than the three slots actually offered, because the student
    # may reasonably have asked for "the one after that".
    if proposed_time not in set(available_slots(count=64)):
        raise NoSuchSlot(
            f"{proposed_time:%Y-%m-%d %H:%M} is not an available advising slot."
        )

    advisor = session.scalar(
        select(Student.advisor_name).where(Student.student_id == student_id)
    )
    if advisor is None:
        raise LookupError(f"No student {student_id}")

    existing = session.scalar(
        select(AdvisorAppointment).where(
            AdvisorAppointment.student_id == student_id,
            AdvisorAppointment.proposed_time == proposed_time,
        )
    )
    if existing is not None:
        # Idempotent: a model that calls the confirmation tool twice for the same
        # agreed slot should not produce two appointments, and should not error
        # either -- the student's intent was satisfied the first time.
        return existing

    appointment = AdvisorAppointment(
        student_id=student_id,
        advisor_name=advisor,
        proposed_time=proposed_time,
        reason=reason.strip(),
        status="confirmed",
        conversation_id=conversation_id,
        confirmed_at=datetime.now(timezone.utc),
    )
    session.add(appointment)
    session.commit()
    return appointment


def for_student(session: Session, student_id: str) -> list[AdvisorAppointment]:
    """This student's appointments, soonest first. Scoped in SQL, as ever."""
    return list(
        session.scalars(
            select(AdvisorAppointment)
            .where(AdvisorAppointment.student_id == student_id)
            .order_by(AdvisorAppointment.proposed_time)
        ).all()
    )
