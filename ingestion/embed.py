"""Turning text into vectors, with OpenAI `text-embedding-3-small`.

The model choice is locked in CLAUDE.md section 3 and argued in DESIGN.md: at two
PDFs and eleven pages the whole corpus is a single API call, so self-hosting
would buy nothing and would put ~2GB of torch into the backend image.

The one thing this module insists on is that **the query and the documents are
embedded by the same function**. Cosine distance between vectors from two
different models is a number with no meaning, and the failure is silent -- you
get results, they are simply wrong. So there is one entry point, used by both the
ingestion pipeline and the retrieval layer.
"""

from __future__ import annotations

import re

from openai import OpenAI

from app.config import get_settings
from app.models import EMBEDDING_DIMENSIONS


class MissingAPIKey(RuntimeError):
    """Raised when embedding is attempted with no API key configured.

    Its own type so callers can distinguish "not configured" from "the API
    failed": the first is a setup problem with a one-line fix, and a generic
    error message sends people looking for a bug instead.
    """


# Pydantic AI Gateway keys encode their region, which is how the base URL is
# derived. Mirrors PydanticAI's own inference in
# `pydantic_ai.providers.gateway`; duplicated rather than imported because that
# helper is private, and rather than borrowing the provider's client because
# ours is synchronous and the provider's is not.
_GATEWAY_KEY = re.compile(r"^pylf_v\d+_(?P<region>[a-z]+(?:-\w+)?)_")


def _gateway_base_url(api_key: str) -> str:
    settings = get_settings()
    if settings.pydantic_ai_gateway_base_url:
        return settings.pydantic_ai_gateway_base_url.rstrip("/")

    match = _GATEWAY_KEY.match(api_key)
    if match is None:
        raise MissingAPIKey(
            "PYDANTIC_AI_GATEWAY_API_KEY does not encode a region, so the gateway "
            "URL cannot be derived. Set PYDANTIC_AI_GATEWAY_BASE_URL explicitly, "
            "or generate a new key."
        )
    region = match.group("region")
    host = (
        "https://gateway.pydantic.info"
        if region.startswith("staging")
        else f"https://gateway-{region}.pydantic.dev"
    )
    return f"{host}/proxy"


def _client() -> OpenAI:
    """An OpenAI-API client, pointed at whichever provider is configured.

    Two supported setups, checked in this order:

    1. **Pydantic AI Gateway** (`PYDANTIC_AI_GATEWAY_API_KEY`). Its OpenAI route
       is API-compatible, so the same client works unchanged against a different
       base URL -- which means one gateway key covers both embeddings and chat.
       Preferred when present, because a stack configured for the gateway should
       not silently bill a stray direct OpenAI key.
    2. **OpenAI directly** (`OPENAI_API_KEY`).

    The embedding *model* is the same either way, so vectors from the two routes
    are interchangeable and switching does not require a re-embed.
    """
    settings = get_settings()

    if settings.pydantic_ai_gateway_api_key:
        key = settings.pydantic_ai_gateway_api_key
        return OpenAI(api_key=key, base_url=f"{_gateway_base_url(key)}/openai")

    if settings.openai_api_key:
        return OpenAI(api_key=settings.openai_api_key)

    raise MissingAPIKey(
        "No embedding credentials configured. Embeddings use "
        f"{settings.embedding_model} (CLAUDE.md section 3), so document ingestion "
        "and search need one of:\n"
        "  PYDANTIC_AI_GATEWAY_API_KEY  (a pylf_... key; also covers chat)\n"
        "  OPENAI_API_KEY               (an sk-... key)\n"
        "Set one in .env and run `docker compose up -d`. Every record endpoint "
        "and the whole portal keep working without it."
    )


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts, preserving order.

    Sent as one request: the entire corpus is under sixty chunks, well inside the
    API's batch limit, and one round trip makes a full re-ingestion effectively
    instant -- which is what lets the admin panel's re-ingest button be a button
    rather than a job queue.
    """
    if not texts:
        return []

    settings = get_settings()
    response = _client().embeddings.create(model=settings.embedding_model, input=texts)
    # The API documents that results come back in input order; asserting it is
    # cheap and the alternative is chunks silently wearing each other's vectors.
    vectors = [item.embedding for item in sorted(response.data, key=lambda d: d.index)]

    if len(vectors) != len(texts):
        raise RuntimeError(
            f"embedded {len(vectors)} vectors for {len(texts)} texts"
        )
    for vector in vectors:
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise RuntimeError(
                f"{settings.embedding_model} returned {len(vector)} dimensions, but "
                f"document_chunks.embedding is VECTOR({EMBEDDING_DIMENSIONS}). "
                "Changing the embedding model needs an Alembic revision and a "
                "full re-ingestion."
            )
    return vectors


def embed_query(query: str) -> list[float]:
    """Embed one search query, through the same path as the documents."""
    return embed_texts([query])[0]
