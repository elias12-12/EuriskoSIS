"""Runtime configuration, read from the environment.

Per CLAUDE.md section 8: secrets and config live in the environment / `.env`,
never hardcoded. `database_url` deliberately has no default -- a missing value
should crash at import time with a clear error rather than silently falling back
to some localhost guess that happens to work on one machine and not in Docker.
"""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

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
    # Which term "my schedule" and load/probation limits refer to. Configuration
    # rather than a literal in queries: it is an operational fact that changes
    # every few months, and `WHERE term_code = 'FA2026'` scattered through the
    # data layer is the kind of thing that rots silently.
    current_term: str = "FA2026"
    # Where the three source files live. Defaults to the container mount point;
    # override with DATA_DIR when running on the host.
    data_dir: Path = Path("/data")

    # --- Handbook academic policy -------------------------------------------
    # These are institutional rules quoted from the Student Handbook, not tuning
    # knobs. They live here rather than as literals scattered through queries so
    # that a policy change is one edit with one place to check, and so the values
    # are visible next to their justification.
    #
    # Deliberately NOT in `assistant_settings`: that table is the admin panel's
    # behaviour config (tone, model, length, temperature) per CLAUDE.md section 5.
    # Letting an admin edit degree rules through the same form would make wrong
    # graduation answers a supported feature.

    # Handbook: a prerequisite must have been passed at C- (1.7) or above. A D
    # earns credit but does not unlock the next course. Distinct from the
    # per-category gate on program_requirement_categories.min_grade_points, which
    # governs whether a course counts toward Major Core.
    prerequisite_min_grade_points: Decimal = Decimal("1.7")
    # Handbook: no course may be attempted more than three times.
    max_course_attempts: int = 3
    # Handbook: full-time is 9-15 credits in Fall/Spring.
    full_time_min_credits: int = 9
    standard_max_credits: int = 15
    # Handbook: academic probation caps registration at 9 credits.
    probation_max_credits: int = 9
    # Handbook: more than 15 credits needs advisor approval AND a GPA of 3.00+.
    overload_min_gpa: Decimal = Decimal("3.00")


@lru_cache
def get_settings() -> Settings:
    """Cached so the env is parsed once per process, not once per request."""
    return Settings()
