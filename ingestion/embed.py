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

from openai import OpenAI

from app.config import get_settings
from app.models import EMBEDDING_DIMENSIONS


class MissingAPIKey(RuntimeError):
    """Raised when embedding is attempted with no OPENAI_API_KEY configured.

    Its own type so callers can distinguish "not configured" from "the API
    failed": the first is a setup problem with a one-line fix, and a generic
    error message sends people looking for a bug instead.
    """


def _client() -> OpenAI:
    api_key = get_settings().openai_api_key
    if not api_key:
        raise MissingAPIKey(
            "OPENAI_API_KEY is not set. Embeddings use OpenAI "
            f"{get_settings().embedding_model} (CLAUDE.md section 3), so document "
            "ingestion and search need it. Copy .env.example to .env and set the "
            "key, then `docker compose up -d` to pick it up. Every Phase 2 "
            "endpoint keeps working without it."
        )
    return OpenAI(api_key=api_key)


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
