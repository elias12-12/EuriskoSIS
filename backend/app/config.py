"""Runtime configuration, read from the environment.

Per CLAUDE.md section 8: secrets and config live in the environment / `.env`,
never hardcoded. `database_url` deliberately has no default -- a missing value
should crash at import time with a clear error rather than silently falling back
to some localhost guess that happens to work on one machine and not in Docker.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Loaded when running outside Docker (`uv run uvicorn ...`). Inside a
        # container the compose file supplies these as real env vars, which take
        # precedence over any file.
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Eurisko University Assistant"
    # SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@db:5432/eurisko
    database_url: str


@lru_cache
def get_settings() -> Settings:
    """Cached so the env is parsed once per process, not once per request."""
    return Settings()
