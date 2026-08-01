"""The ingestion pipeline: PDF in, embedded and searchable chunks out.

Re-runnable by design, keyed on `documents.filename`. PROJECT_PLAN Phase 3 asks
for delete-and-reinsert per document so the admin panel's "re-run ingestion"
button has something real to call, and the same property is what makes iterating
on a chunker cheap -- change a boundary, re-run, look at the result.

Two failure behaviours worth stating, because both were choices:

**A failed ingestion leaves no partial chunks, but does leave a record.** The
chunk write happens in one transaction that is rolled back on any error; the
`documents` row is then updated separately to `failed` with the message. A
half-ingested document that still answers questions is far worse than one that
visibly refuses to -- it would cite a section it no longer fully contains.

**A document is not searchable until it is `ready`.** Retrieval filters on
status, so the window between deleting the old chunks and committing the new ones
cannot serve a partially rebuilt corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk
from ingestion.embed import embed_texts
from ingestion.parse import CHUNKERS, parse


@dataclass(frozen=True)
class IngestResult:
    filename: str
    status: str
    chunk_count: int
    page_count: int | None
    unchanged: bool
    error: str | None = None


def ingest_document(
    session: Session, path: Path, *, force: bool = False
) -> IngestResult:
    """Parse, chunk, embed and store one document, replacing any previous run.

    `force=False` skips a document whose bytes are unchanged and whose chunks are
    already `ready`. Re-embedding identical text would produce identical vectors
    at a real cost in API calls, and, more importantly, the skip is what makes it
    safe to call this on every startup or button press without thinking about it.
    Pass `force=True` after changing a chunker -- the file is then unchanged but
    the chunks are not.
    """
    document = session.scalar(select(Document).where(Document.filename == path.name))
    extracted, chunks = parse(path)

    if (
        not force
        and document is not None
        and document.sha256 == extracted.sha256
        and document.status == "ready"
    ):
        return IngestResult(
            filename=path.name,
            status="ready",
            chunk_count=len(document.chunks),
            page_count=document.page_count,
            unchanged=True,
        )

    if document is None:
        document = Document(filename=path.name, title=extracted.title, status="pending")
        session.add(document)
        session.flush()

    document.title = extracted.title
    document.page_count = extracted.page_count
    document.sha256 = extracted.sha256
    document.status = "ingesting"
    document.error = None
    session.commit()

    try:
        # Old chunks go before the new ones are embedded, not after: the unique
        # constraint on (document_id, chunk_index) would otherwise collide with
        # the previous run on every single row.
        session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == document.id)
        )
        vectors = embed_texts([chunk.content for chunk in chunks])
        session.add_all(
            DocumentChunk(
                document_id=document.id,
                chunk_index=index,
                content=chunk.content,
                embedding=vector,
                chunk_kind=chunk.chunk_kind,
                section_ref=chunk.section_ref,
                section_title=chunk.section_title,
                page=chunk.page,
            )
            for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
        )
        document.status = "ready"
        session.commit()
    except Exception as exc:
        session.rollback()
        # A second, independent transaction: the point of this record is to
        # survive the rollback that just discarded the chunks.
        document = session.scalar(
            select(Document).where(Document.filename == path.name)
        )
        if document is not None:
            document.status = "failed"
            document.error = f"{type(exc).__name__}: {exc}"
            session.commit()
        return IngestResult(
            filename=path.name,
            status="failed",
            chunk_count=0,
            page_count=extracted.page_count,
            unchanged=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    return IngestResult(
        filename=path.name,
        status="ready",
        chunk_count=len(chunks),
        page_count=extracted.page_count,
        unchanged=False,
    )


def ingest_all(
    session: Session, data_dir: Path, *, force: bool = False
) -> list[IngestResult]:
    """Ingest every document the assistant is allowed to know things from.

    Driven by the registered chunkers rather than by globbing the directory:
    CLAUDE.md section 4 fixes the corpus at these files, and a stray PDF dropped
    into `data/` must not become a source the assistant will cite.
    """
    results = []
    for filename in CHUNKERS:
        path = data_dir / filename
        if not path.exists():
            results.append(
                IngestResult(
                    filename=filename,
                    status="failed",
                    chunk_count=0,
                    page_count=None,
                    unchanged=False,
                    error=f"not found at {path}",
                )
            )
            continue
        results.append(ingest_document(session, path, force=force))
    return results
