"""Vector search over the ingested documents.

This is the data layer behind the `search_documents` tool of CLAUDE.md section 6,
built and tested here without an agent (PROJECT_PLAN Phase 3) so that when the
agent arrives the only new thing is the agent.

Design notes:

**Cosine distance, and no vector index.** OpenAI returns normalised vectors, so
cosine ranks identically to dot product; cosine is used because it is what
pgvector's `<=>` operator and the eventual index would both assume. There is no
index on the column -- at fifty-eight chunks an exact sequential scan is faster
than an approximate structure and, unlike IVFFlat, cannot miss the true nearest
neighbour. See the note on `DocumentChunk`.

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

    limit = top_k or get_settings().retrieval_top_k
    vector = embed_query(query)
    distance = DocumentChunk.embedding.cosine_distance(vector)

    rows = session.execute(
        select(
            DocumentChunk.content,
            Document.filename,
            Document.title,
            DocumentChunk.chunk_kind,
            DocumentChunk.section_ref,
            DocumentChunk.section_title,
            DocumentChunk.page,
            distance.label("distance"),
        )
        .join(Document, DocumentChunk.document_id == Document.id)
        .where(Document.status == "ready", DocumentChunk.embedding.is_not(None))
        .order_by(distance)
        .limit(limit)
    ).all()

    return [
        RetrievedChunk(
            content=row.content,
            document_filename=row.filename,
            document_title=row.title,
            chunk_kind=row.chunk_kind,
            section_ref=row.section_ref,
            section_title=row.section_title,
            page=row.page,
            # Cosine distance is 0 (identical) to 2 (opposite); similarity is the
            # more readable direction for a log line or an admin panel column.
            similarity=1.0 - float(row.distance),
        )
        for row in rows
    ]


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
