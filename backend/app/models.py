"""SQLAlchemy declarative base and ORM models.

Intentionally has no tables yet. Phase 0's job is only to make the metadata
object exist and be importable, because Alembic's autogenerate diffs this
metadata against the live database -- the real schema (CLAUDE.md section 5)
lands in Phase 1.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
