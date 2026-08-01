"""Document retrieval endpoints -- Phase 3: plain HTTP, no agent.

Same pattern as the Phase 2 student endpoints: build the data layer as an
endpoint first, verify it by hand through Swagger, and only then wrap it as a
PydanticAI tool. `GET /documents/search` is what `search_documents` will call in
Phase 4; the tool adds no logic of its own.

Unlike the student endpoints these need no scoping. The corpus is institutional
policy, identical for every student, so there is no per-student surface to get
wrong here -- CLAUDE.md section 6 marks `search_documents` as the one tool that
is not scoped.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_session
from app.retrieval import corpus_status, search
from app.schemas import DocumentStatus, SearchHit, SearchResults
from ingestion.embed import MissingAPIKey

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("", response_model=list[DocumentStatus])
def list_documents(session: Session = Depends(get_session)) -> list[DocumentStatus]:
    """What has been ingested, and whether it is searchable.

    The first thing to check when search returns nothing: an empty corpus and a
    broken query look identical from the caller's side.
    """
    return [DocumentStatus(**row) for row in corpus_status(session)]


@router.get("/search", response_model=SearchResults)
def search_documents(
    q: str = Query(
        description="Natural-language question about policy, deadlines, fees or courses.",
        examples=["When is the last day to drop a course without a W?"],
        min_length=1,
    ),
    top_k: int = Query(
        default=5,
        ge=1,
        le=20,
        description="How many chunks to return, closest first.",
    ),
    session: Session = Depends(get_session),
) -> SearchResults:
    """Nearest chunks to the query, each with the citation an answer must quote."""
    try:
        hits = search(session, q, top_k=top_k)
    except MissingAPIKey as exc:
        # 503, not 500: the service is correctly built and temporarily unable to
        # do this, and the message says exactly what is missing.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return SearchResults(
        query=q,
        hits=[
            SearchHit(
                content=hit.content,
                document_title=hit.document_title,
                document_filename=hit.document_filename,
                chunk_kind=hit.chunk_kind,
                section_ref=hit.section_ref,
                section_title=hit.section_title,
                page=hit.page,
                similarity=hit.similarity,
                citation=hit.citation(),
            )
            for hit in hits
        ],
    )
