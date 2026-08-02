"""Session memory: persisting a conversation and replaying it to the model.

CLAUDE.md section 7 rule 3 wants follow-ups to resolve from context without the
user repeating themselves. That needs the *previous run's* messages back in the
next request, including the tool calls -- "what about next term?" only works if
the model can see what it already looked up.

So history is stored as the serialised PydanticAI `ModelMessage` list and
replayed verbatim, with `role`/`content` kept alongside as the human-readable
projection for the Phase 6 chat panel. See the `Message` model for why both.

**A conversation is student-scoped data.** `load_for_student` refuses a thread
belonging to someone else, with the same firmness as any other record access:
a transcript contains grades, schedules and degree progress, so handing one over
because a client passed a different `conversation_id` would defeat every other
scoping rule in the app. This is the one place where session memory could quietly
become a hole, so the check is here rather than in the route.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from app.config import get_settings
from app.models import Conversation, Message


class ConversationNotFound(LookupError):
    """No such conversation, or it belongs to a different student.

    One exception for both cases on purpose: distinguishing them would confirm
    that a given conversation id exists and belongs to somebody, which is more
    than a student asking about a thread that is not theirs should learn.
    """


def start(session: Session, student_id: str | None) -> Conversation:
    conversation = Conversation(student_id=student_id)
    session.add(conversation)
    session.commit()
    return conversation


def load_for_student(
    session: Session, conversation_id: int, student_id: str
) -> Conversation:
    """Fetch a conversation, or raise if it is not this student's."""
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.student_id != student_id:
        raise ConversationNotFound(conversation_id)
    return conversation


def history(session: Session, conversation_id: int) -> list[ModelMessage]:
    """The stored messages, ready to hand back to `Agent.run(message_history=...)`.

    Trimmed to the most recent `conversation_history_limit` rows and then
    restored to chronological order -- the model needs the *latest* context, but
    it needs it in the order it happened.
    """
    limit = get_settings().conversation_history_limit
    rows = session.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.model_message.is_not(None),
        )
        .order_by(Message.id.desc())
        .limit(limit)
    ).all()

    messages: list[ModelMessage] = []
    for row in reversed(rows):
        # Validated one row at a time: a single unreadable row (from an older
        # PydanticAI schema, say) should cost that turn of context, not the
        # whole conversation.
        messages.extend(ModelMessagesTypeAdapter.validate_python([row.model_message]))
    return messages


def record_turn(
    session: Session,
    conversation_id: int,
    *,
    user_text: str,
    assistant_text: str,
    new_messages: list[ModelMessage],
) -> None:
    """Store one exchange: the display projection and the replayable messages.

    Written in a single transaction so the two representations cannot disagree
    about what happened. `new_messages` is `AgentRunResult.new_messages()`, which
    is the run's own record of this turn -- request, tool calls, tool returns and
    response -- rather than something reconstructed afterwards.
    """
    serialised = ModelMessagesTypeAdapter.dump_python(new_messages, mode="json")

    # The display rows: exactly two per turn, whatever happened in between.
    session.add(
        Message(
            conversation_id=conversation_id,
            role="user",
            content=user_text,
            model_message=serialised[0] if serialised else None,
        )
    )
    for message in serialised[1:-1]:
        # Tool calls and their results: no display text of their own, but they
        # must be replayed, so they are stored with the assistant's role and an
        # empty-but-descriptive content rather than dropped.
        session.add(
            Message(
                conversation_id=conversation_id,
                role="assistant",
                content="",
                model_message=message,
            )
        )
    session.add(
        Message(
            conversation_id=conversation_id,
            role="assistant",
            content=assistant_text,
            model_message=serialised[-1] if len(serialised) > 1 else None,
        )
    )
    session.commit()


def transcript(session: Session, conversation_id: int) -> list[Message]:
    """The human-readable thread, for the Phase 6 chat panel.

    Empty-content rows are the tool-call messages stored for replay; they are
    filtered here rather than at write time, because dropping them would break
    the replay that is the whole point of storing them.
    """
    rows = session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id)
    ).all()
    return [row for row in rows if row.content]
