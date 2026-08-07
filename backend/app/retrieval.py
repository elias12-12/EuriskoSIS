"""Vector search over the ingested documents.

This is the data layer behind the `search_documents` tool of CLAUDE.md section 6,
built and tested here without an agent (PROJECT_PLAN Phase 3) so that when the
agent arrives the only new thing is the agent.

Design notes:

**Hybrid: vector plus lexical, fused by reciprocal rank.** Retrieval began as
pure vector search, with the note that a lexical channel was "the obvious upgrade
if an exact-code query ranks a neighbouring course first -- but that is a
hypothesis, and the six-question test set is what decides it".

The test set decided it. Pure vector search failed three of the six, and every
failure was a case where the right passage contains the query's words verbatim
while the embedding could not separate it from its neighbours:

- *"prerequisites for CENG 320"* returned CENG 420, then CENG 330, with CENG 320
  only third. Thirty-three course entries share a template, a subject prefix and
  a length; the digits are most of the signal and are exactly what a 1536-vector
  average smooths away.
- *"last day to drop a course without a W"* returned section 2.3, which gives the
  rule ("end of the third week"), over section 5, which gives the date -- even
  though section 5 contains that phrase word for word.
- *"who do I contact about a scholarship"* returned section 7, which describes
  the awards and lists no contact at all, over section 9's routing table.

So a Postgres full-text channel runs alongside the vector one and the two
rankings are fused with **reciprocal rank fusion**: each chunk scores
`sum(1 / (k + rank))` over the rankers that returned it. RRF is used rather than
a weighted sum of scores because cosine similarity and `ts_rank_cd` are not on
comparable scales, and normalising them would mean inventing a conversion and
then tuning it. Ranks need no such calibration, and a chunk that both channels
like rises above one that only a single channel loves.

**Cosine distance, and no index on either channel.** OpenAI returns normalised
vectors, so cosine ranks identically to dot product; cosine is used because it is
what pgvector's `<=>` operator and any eventual index would assume. Neither the
embedding nor the text has an index: at fifty-eight chunks an exact scan is
faster than an approximate structure and, unlike IVFFlat, cannot miss the true
nearest neighbour, and `to_tsvector` over 58 short rows is not measurable.

**Only `ready` documents are searchable.** A document mid-re-ingestion has an
incomplete chunk set, and answering from it would produce a citation to a
section the corpus no longer fully contains.

**Every result carries its citation.** CLAUDE.md section 7 rule 5 requires the
document and section behind every document-based answer, so the query returns
them alongside the text rather than leaving the caller to look them up.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Document, DocumentChunk
from ingestion.embed import embed_query


@dataclass(frozen=True)
class RetrievedChunk:
    """One search hit, with everything an answer needs to cite it."""

    content: str
    document_filename: str
    document_title: str
    chunk_kind: str
    section_ref: str | None
    section_title: str | None
    page: int | None
    similarity: float
    # Which channel(s) surfaced this: 'vector', 'lexical' or 'both'. Kept because
    # "why did that come back?" is the first question when a result looks odd,
    # and with two rankers the answer is no longer obvious from the score.
    matched_by: str = "vector"

    def citation(self) -> str:
        """The source string an answer should quote, e.g.
        'Student Handbook 2026-2027, section 2.3 (Adding, dropping and
        withdrawing), page 2'.
        """
        parts = [self.document_title]
        if self.section_ref:
            title = f" ({self.section_title})" if self.section_title else ""
            parts.append(f"section {self.section_ref}{title}")
        elif self.section_title:
            parts.append(self.section_title)
        if self.page is not None:
            parts.append(f"page {self.page}")
        return ", ".join(parts)


def search(
    session: Session, query: str, *, top_k: int | None = None
) -> list[RetrievedChunk]:
    """Return the chunks nearest to `query`, closest first.

    Not scoped to a student, and deliberately so: the documents are institutional
    policy, identical for everyone. Personal scoping lives on the other tools
    (CLAUDE.md section 6), and conflating the two would be the wrong place to
    enforce it.
    """
    if not query.strip():
        return []

    settings = get_settings()
    limit = top_k or settings.retrieval_top_k
    candidates = max(limit * 4, settings.retrieval_candidates)

    vector_ranked = _vector_candidates(session, embed_query(query), candidates)
    lexical_ranked = _lexical_candidates(session, query, candidates)

    order = _fuse(vector_ranked, lexical_ranked)[:limit]
    if not order:
        return []

    return _hydrate(session, order, vector_ranked, lexical_ranked)


def _vector_candidates(
    session: Session, vector: list[float], limit: int
) -> dict[int, float]:
    """Chunk id -> cosine similarity, nearest first. Insertion order is the rank."""
    distance = DocumentChunk.embedding.cosine_distance(vector)
    rows = session.execute(
        select(DocumentChunk.id, distance.label("distance"))
        .join(Document, DocumentChunk.document_id == Document.id)
        .where(Document.status == "ready", DocumentChunk.embedding.is_not(None))
        .order_by(distance)
        .limit(limit)
    ).all()
    # Cosine distance is 0 (identical) to 2 (opposite); similarity is the more
    # readable direction for a log line or an admin panel column.
    return {row.id: 1.0 - float(row.distance) for row in rows}


def _lexical_candidates(session: Session, query: str, limit: int) -> dict[int, float]:
    """Chunk id -> ts_rank_cd, best first.

    `websearch_to_tsquery` rather than `plainto_tsquery`: it accepts the way a
    person actually types a question, including quoted phrases, and it does not
    fall over on punctuation. An unparseable query simply yields no rows, which
    degrades to pure vector search rather than to an error.
    """
    tsquery = func.websearch_to_tsquery("english", query)
    tsvector = func.to_tsvector("english", DocumentChunk.content)
    rank = func.ts_rank_cd(tsvector, tsquery)

    rows = session.execute(
        select(DocumentChunk.id, rank.label("rank"))
        .join(Document, DocumentChunk.document_id == Document.id)
        .where(Document.status == "ready", tsvector.op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(limit)
    ).all()
    return {row.id: float(row.rank) for row in rows}


# Smoothing constant for reciprocal rank fusion. 60 is the value from the
# original RRF paper and is deliberately large relative to our candidate list:
# it flattens the difference between ranks 1 and 2 so that agreement between the
# two channels matters more than either channel's confidence in its own top hit.
_RRF_K = 60


def _fuse(*rankings: dict[int, float]) -> list[int]:
    """Reciprocal rank fusion over any number of ranked candidate lists."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
    # Ties broken by chunk id so the order is deterministic across runs -- a test
    # that sometimes passes is worse than one that fails.
    return sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))


def _hydrate(
    session: Session,
    order: list[int],
    vector_ranked: dict[int, float],
    lexical_ranked: dict[int, float],
) -> list[RetrievedChunk]:
    """Fetch the chosen chunks and return them in the fused order.

    One query for the page rather than one per chunk, then reordered in Python:
    SQL has no natural way to sort by an arbitrary externally-computed sequence
    without a VALUES join, and at five rows that is not worth the SQL.
    """
    rows = session.execute(
        select(
            DocumentChunk.id,
            DocumentChunk.content,
            Document.filename,
            Document.title,
            DocumentChunk.chunk_kind,
            DocumentChunk.section_ref,
            DocumentChunk.section_title,
            DocumentChunk.page,
        )
        .join(Document, DocumentChunk.document_id == Document.id)
        .where(DocumentChunk.id.in_(order))
    ).all()
    by_id = {row.id: row for row in rows}

    hits = []
    for chunk_id in order:
        row = by_id.get(chunk_id)
        if row is None:
            continue
        in_vector = chunk_id in vector_ranked
        in_lexical = chunk_id in lexical_ranked
        hits.append(
            RetrievedChunk(
                content=row.content,
                document_filename=row.filename,
                document_title=row.title,
                chunk_kind=row.chunk_kind,
                section_ref=row.section_ref,
                section_title=row.section_title,
                page=row.page,
                # A lexical-only hit has no cosine similarity of its own; -1 is
                # reported rather than 0.0 so it is visibly "not measured"
                # instead of looking like a genuinely orthogonal match.
                similarity=vector_ranked.get(chunk_id, -1.0),
                matched_by=(
                    "both"
                    if in_vector and in_lexical
                    else "vector"
                    if in_vector
                    else "lexical"
                ),
            )
        )
    return hits


def corpus_status(session: Session) -> list[dict[str, object]]:
    """What has been ingested, for the admin panel and for diagnosing retrieval.

    "Search returns nothing" and "nothing has been ingested" look identical from
    the outside; this is what tells them apart.
    """
    rows = session.execute(
        select(
            Document.filename,
            Document.title,
            Document.status,
            Document.page_count,
            Document.uploaded_at,
            Document.error,
            func.count(DocumentChunk.id).label("chunk_count"),
            # Counted separately from chunk_count: a document can hold chunks that
            # were never embedded if a run failed partway, and "58 chunks, 0
            # searchable" is the diagnosis that a single total would hide.
            func.count(DocumentChunk.embedding).label("embedded_count"),
        )
        # Outer join, so a document with no chunks at all still appears -- that is
        # precisely the state worth seeing.
        .outerjoin(DocumentChunk, DocumentChunk.document_id == Document.id)
        .group_by(
            Document.id,
            Document.filename,
            Document.title,
            Document.status,
            Document.page_count,
            Document.uploaded_at,
            Document.error,
        )
        .order_by(Document.filename)
    ).all()

    return [
        {
            "filename": row.filename,
            "title": row.title,
            "status": row.status,
            "page_count": row.page_count,
            "uploaded_at": row.uploaded_at,
            "chunk_count": row.chunk_count,
            "embedded_count": row.embedded_count,
            "error": row.error,
        }
        for row in rows
    ]
