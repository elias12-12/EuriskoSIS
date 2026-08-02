"""FastAPI application entrypoint.

Phase 0 scope only: the app exists, it starts, and it can prove it reaches
Postgres. No domain routes yet -- those arrive in Phase 2.
"""

from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.routers import auth, documents, me, students

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.4.0",
    summary="University assistant API (Phase 4: agent, session-scoped /me surface)",
)

router = APIRouter(tags=["health"])


@router.get("/")
def root() -> dict[str, str]:
    return {
        "service": settings.app_name,
        "phase": "3 - document ingestion and retrieval, no agent",
        "docs": "/docs",
    }


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness only: is the process up? Says nothing about the database."""
    return {"status": "ok"}


@router.get("/health/db")
def health_db(session: Session = Depends(get_session)) -> Any:
    """Readiness: the Phase 0 exit check, `SELECT 1` against Postgres.

    Also reports the pgvector extension version. "Postgres is reachable" and
    "the extension the whole retrieval layer depends on is installed" are two
    separate failures, and it is much cheaper to tell them apart now than in
    Phase 3 when embeddings stop working.
    """
    try:
        select_1 = session.execute(text("SELECT 1")).scalar_one()
        server_version = session.execute(text("SHOW server_version")).scalar_one()
        pgvector_version = session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one_or_none()
    except SQLAlchemyError as exc:
        # Surface the driver's message rather than a bare 500 -- during Phase 0 the
        # cause is almost always a wrong host or a database still booting, and the
        # message says which.
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "error": str(exc.__cause__ or exc)},
        )

    return {
        "status": "ok",
        "select_1": select_1,
        "postgres_version": server_version,
        "pgvector_installed": pgvector_version is not None,
        "pgvector_version": pgvector_version,
    }


app.include_router(router)
app.include_router(auth.router)
# The student-facing surface. `students.router` below is the admin/by-ID one and
# must not be called by a student's browser -- see the docstring on each.
app.include_router(me.router)
app.include_router(students.router)
app.include_router(documents.router)
