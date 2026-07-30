"""Alembic environment.

Two deviations from the stock template, both deliberate:

1. The connection URL comes from `app.config.Settings` (i.e. DATABASE_URL) rather
   than `alembic.ini`, so migrations and the running app can never disagree about
   which database they point at, and no credential lands in a tracked file.
2. `pgvector.sqlalchemy` is imported for its side effect of registering the
   VECTOR type, so autogenerate compares embedding columns correctly instead of
   proposing to drop and recreate them on every run.
"""

from logging.config import fileConfig

import pgvector.sqlalchemy  # noqa: F401  -- registers VECTOR; see docstring
from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL without connecting (`alembic upgrade head --sql`)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without this, a changed column type is silently ignored by
            # autogenerate -- a quiet way to lose a Phase 1 schema fix.
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
