# backend

FastAPI + PydanticAI service, managed with `uv`. See the
[root README](../README.md) for how to run the stack, and
[CLAUDE.md](../CLAUDE.md) for architecture decisions and the data model.

```
app/       config.py (env settings), db.py (engine/session), main.py (routes),
           models.py (SQLAlchemy declarative base)
alembic/   migrations; versions/0001 enables the pgvector extension
```
