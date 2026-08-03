"""One chat turn: config, memory, agent run, persistence.

Kept out of the router so the sequence is readable in one place, and so the
Phase 6 admin panel can drive the same path without going through HTTP.

The order matters and is the phase's requirement stated as code:

1. Read `assistant_settings` **now**, not at startup. The admin's tone, model,
   length and temperature take effect on this request (PROJECT_PLAN Phase 6).
2. Build `StudentContext` from the *authenticated* ID handed in by the caller.
   This function has no way to obtain a student ID by itself, which is why it
   takes one -- see `auth.current_student`, its only source.
3. Replay the conversation's stored messages so follow-ups resolve.
4. Run the agent with the per-request model, instructions and temperature.
5. Persist the turn in one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import ModelSettings
from pydantic_ai.usage import UsageLimits
from sqlalchemy.orm import Session

from app import assistant_config, conversations
from app.agent import StudentContext, agent
from app.config import get_settings


@dataclass(frozen=True)
class ChatTurn:
    conversation_id: int
    reply: str
    model_name: str
    tool_calls: list[str]


def send(
    session: Session,
    *,
    student_id: str,
    message: str,
    conversation_id: int | None,
) -> ChatTurn:
    """Answer one message as `student_id`, continuing `conversation_id` if given.

    Raises `conversations.ConversationNotFound` if the conversation belongs to
    another student -- checked before anything else happens, so a wrong id costs
    nothing and reveals nothing.
    """
    if conversation_id is None:
        conversation = conversations.start(session, student_id)
    else:
        conversation = conversations.load_for_student(
            session, conversation_id, student_id
        )

    config = assistant_config.load(session)
    settings = get_settings()

    # `run_sync`, not `await run`, and the route handler is a plain `def`. FastAPI
    # runs sync handlers in a threadpool, so this owns an event loop in its own
    # worker thread and the blocking SQLAlchemy calls inside the tools block only
    # that thread. The alternative -- async all the way down -- would mean an
    # async engine and rewriting the Phase 1-3 query layer, which buys concurrency
    # this app has no evidence of needing.
    result = agent.run_sync(
        message,
        deps=StudentContext(
            student_id=student_id,
            session=session,
            conversation_id=conversation.id,
        ),
        model=config.model_name,
        instructions=config.instructions(),
        message_history=conversations.history(session, conversation.id),
        model_settings=ModelSettings(temperature=float(config.temperature)),
        usage_limits=UsageLimits(tool_calls_limit=settings.agent_max_tool_calls),
    )

    new_messages = result.new_messages()
    conversations.record_turn(
        session,
        conversation.id,
        user_text=message,
        assistant_text=result.output,
        new_messages=new_messages,
    )

    return ChatTurn(
        conversation_id=conversation.id,
        reply=result.output,
        model_name=config.model_name,
        # Surfaced in the response because "did it actually look anything up?"
        # is the first question when an answer looks wrong, and reading it off
        # the reply is guesswork.
        tool_calls=_tool_names(new_messages),
    )


def _tool_names(messages: list) -> list[str]:
    names: list[str] = []
    for message in messages:
        for part in getattr(message, "parts", []):
            if part.__class__.__name__ == "ToolCallPart":
                names.append(part.tool_name)
    return names
