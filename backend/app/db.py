"""Database engine and per-request session.

One engine, one pool, for the whole app -- that is the point of the single-Postgres
decision in CLAUDE.md section 3.
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

engine = create_engine(
    get_settings().database_url,
    # The API container reliably wins the startup race against Postgres' first-boot
    # initialisation, and Docker restarts can leave dead connections in the pool.
    # pool_pre_ping spends one cheap round-trip per checkout so that shows up as a
    # transparent reconnect instead of an intermittent 500 on the first request.
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    with SessionLocal() as session:
        yield session
