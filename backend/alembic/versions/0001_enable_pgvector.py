"""Enable the pgvector extension.

The single-Postgres decision (CLAUDE.md section 3) makes pgvector a hard
dependency of the schema, so it is enabled as revision 1 rather than left to
image setup -- a clean database created by anything other than `docker compose up`
must still end up with the extension present.

Revision ID: 0001_enable_pgvector
Revises:
Create Date: 2026-07-30

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0001_enable_pgvector"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")
