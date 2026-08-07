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


class MissingModelKey(RuntimeError):
    """The configured chat model's provider has no API key set.

    Its own type, and checked *before* the agent runs, for the same reason
    `ingestion.embed.MissingAPIKey` exists: without it the provider SDK raises
    while constructing its client, deep inside PydanticAI, and the route turns
    that into a 500. A 500 says "this application is broken"; the truth is "this
    application is not configured", which has a one-line fix.

    Found by actually running the stack with no key -- the search endpoint had
    this guard from Phase 3 and the chat endpoint never did.
    """


# Which environment variable each provider prefix needs. Only the providers this
# project realistically uses; anything else is allowed through and left to fail
# in the SDK, because guessing at a third party's variable name would be worse
# than the SDK's own message.
#
# `gateway/...` is Pydantic AI Gateway, which proxies to an upstream provider --
# so `gateway/openai:gpt-5-mini` needs the *gateway* key, not an OpenAI one.
# Matched on the prefix before the slash for exactly that reason.
_PROVIDER_KEYS = {
    "gateway": ("PYDANTIC_AI_GATEWAY_API_KEY", "pydantic_ai_gateway_api_key"),
    "openai": ("OPENAI_API_KEY", "openai_api_key"),
    "anthropic": ("ANTHROPIC_API_KEY", "anthropic_api_key"),
}


def _require_model_credentials(model_name: str) -> None:
    """Fail early, and in a way the UI can explain, if the model has no key."""
    if model_name.startswith("gateway/"):
        provider = "gateway"
    elif ":" in model_name:
        provider = model_name.split(":", 1)[0]
    else:
        provider = "openai"

    entry = _PROVIDER_KEYS.get(provider)
    if entry is None:
        return

    variable, attribute = entry
    if getattr(get_settings(), attribute, None):
        return

    # The message names the alternative too. A stack holding only a gateway key
    # while the settings row still says `openai:...` is a realistic and
    # confusing state, and "set OPENAI_API_KEY" would be the wrong advice for it
    # -- the fix is to change the model in the admin panel.
    alternatives = [
        name
        for provider_name, (name, attribute_name) in _PROVIDER_KEYS.items()
        if provider_name != provider and getattr(get_settings(), attribute_name, None)
    ]
    hint = (
        f" You do have {' and '.join(alternatives)} set, so the quicker fix may be "
        f"to change the model in the admin panel to one that provider serves."
        if alternatives
        else ""
    )

    raise MissingModelKey(
        f"{variable} is not set, and the assistant is configured to use "
        f"{model_name!r}.{hint} Set the key in .env and restart "
        f"(`docker compose up -d`). Every other part of the portal -- profile, "
        f"schedule, academic history, degree progress and eligibility -- works "
        f"without it."
    )


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
    another student -- checked first, so a wrong id costs nothing and reveals
    nothing regardless of how the rest of the stack is configured.

    Raises `MissingModelKey` if the configured model's provider has no API key.
    """
    # Ownership first: it is the security check, and its answer must not depend
    # on whether a model key happens to be configured.
    existing = (
        None
        if conversation_id is None
        else conversations.load_for_student(session, conversation_id, student_id)
    )

    config = assistant_config.load(session)
    settings = get_settings()

    # Then credentials, before any conversation is *created* -- otherwise every
    # failed attempt on a misconfigured stack leaves an empty thread behind.
    _require_model_credentials(config.model_name)

    conversation = existing or conversations.start(session, student_id)

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
