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
    # Where admin-uploaded replacements go. Separate from `data_dir` because that
    # is mounted read-only on purpose -- the dataset is frozen, and an upload
    # must never overwrite it. Ingestion prefers a file here when one exists, so
    # deleting the upload reverts to the shipped document.
    upload_dir: Path = Path("/uploads")

    # --- Retrieval (Phase 3) -------------------------------------------------
    # Locked in CLAUDE.md section 3. The vector *dimension* is not here: it is a
    # schema fact (`models.EMBEDDING_DIMENSIONS`), because changing it requires a
    # migration and a re-embed, not a restart.
    embedding_model: str = "text-embedding-3-small"
    # Unlike `database_url` this may be absent, and absence is not a startup
    # error: the API serves every Phase 2 endpoint without it. It fails at the
    # first embedding call instead, where the message can say what is missing.
    openai_api_key: str | None = None
    # Only needed when the admin points `assistant_settings.model_name` at an
    # anthropic:* model. Declared here so the chat path can check for the key its
    # *configured* provider needs, rather than assuming OpenAI.
    anthropic_api_key: str | None = None
    # Pydantic AI Gateway: one key that proxies to several upstream providers.
    # Supported natively by PydanticAI (`gateway/openai:gpt-5-mini`) and, because
    # its OpenAI route is API-compatible, usable for embeddings too -- so a single
    # gateway key runs both halves of this application. Verified against the live
    # gateway: chat completions and text-embedding-3-small at 1536 dimensions.
    pydantic_ai_gateway_api_key: str | None = None
    # Normally derived from the region encoded in the key; set only to override.
    pydantic_ai_gateway_base_url: str | None = None
    # How many chunks a search returns. Five is enough for a cited answer over a
    # corpus of roughly seventy chunks without burying the model in near-misses.
    retrieval_top_k: int = 5
    # How many candidates each channel contributes to the fusion before the top-k
    # is taken. Wider than top_k on purpose: a chunk that one channel ranks 12th
    # and the other ranks 2nd should still be able to win, and a narrow candidate
    # list would have discarded it before the fusion ever saw it.
    retrieval_candidates: int = 25

    # --- Agent layer (Phase 4) ----------------------------------------------
    # Session lifetime. Login is a student ID with no secret, so this is not
    # protecting much -- it exists so abandoned sessions do not accumulate
    # indefinitely, not as a security control.
    session_ttl_hours: int = 12
    # How many past messages of a conversation are replayed to the model. The
    # cap is on turns rather than tokens because it is the honest unit here: a
    # thread is a handful of short exchanges, and a token budget would be a
    # guess dressed up as precision. Raise it if follow-ups start losing context.
    conversation_history_limit: int = 40
    # Ceiling on tool calls in one agent run. A model that loops between
    # `search_documents` and `get_my_courses` should fail visibly rather than
    # bill quietly.
    agent_max_tool_calls: int = 8

    # --- Admin panel (Phase 6) ----------------------------------------------
    # The brief gives the admin "a separate, simpler login": one shared password,
    # no per-administrator accounts. Required, with no default, for the same
    # reason `database_url` has none -- a default admin password is a credential
    # that ships. Compose supplies a throwaway dev value inline so a clean clone
    # still runs, exactly as it does for the Postgres credentials.
    admin_password: str
    admin_session_ttl_hours: int = 8
    # Origins the browser app is served from. The Vite dev server runs on a
    # different port from the API, so without this every request fails CORS.
    # A list rather than "*" because credentials are sent on these requests.
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

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
