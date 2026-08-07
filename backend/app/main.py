"""FastAPI application entrypoint.

Phase 0 scope only: the app exists, it starts, and it can prove it reaches
Postgres. No domain routes yet -- those arrive in Phase 2.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.routers import admin, auth, documents, me, students

logger = logging.getLogger("app")

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.6.0",
    summary="University assistant API (Phase 6: student portal and admin panel)",
)

# The React app is served by Vite on a different port, so without this every
# browser request fails preflight. An explicit origin list rather than "*":
# these requests carry an Authorization header, and a wildcard origin with
# credentials is both refused by browsers and the wrong thing to want.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """Return a JSON 500 that still carries CORS headers.

    Without this, an unhandled exception is turned into a response by Starlette's
    `ServerErrorMiddleware`, which sits *outside* `CORSMiddleware` -- so the 500
    arrives with no `Access-Control-Allow-Origin` and the browser refuses to read
    it. The frontend then reports `TypeError: Failed to fetch`, which says
    nothing at all: no status, no message, and it looks like the API is down
    rather than like one endpoint raised.

    Registering a handler here puts the response back inside the middleware
    stack, so it comes back out through CORS like any other. The traceback is
    still logged; only the browser's view of it improves.

    Found the hard way, from a chat request failing with `Failed to fetch` while
    every other endpoint worked.
    """
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": (
                f"{type(exc).__name__}: {exc}. See the backend logs for the "
                "traceback (`docker compose logs backend`)."
            )
        },
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
app.include_router(admin.router)
