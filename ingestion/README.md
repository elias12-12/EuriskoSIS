# ingestion

PDF parsing, chunking, and embedding. Built in Phase 3 (see
[PROJECT_PLAN.md](../PROJECT_PLAN.md)). The chunking strategy is summarised in
[DESIGN.md](../DESIGN.md) §2 and argued at length, with the rejected
alternatives, in [DESIGN_NOTES.md](../DESIGN_NOTES.md).

Lives at the repo root but is copied into the backend image at `/app/ingestion`
and imported as `ingestion`, so the admin panel's "re-run ingestion" button can
call `pipeline.ingest_all` in-process rather than shelling out. The backend's
Docker build context is therefore the repo root, not `backend/`.

| Module | What it does |
|---|---|
| `extract.py` | PDF → page-tagged line stream, running headers stripped |
| `catalogue.py` | Catalogue → one chunk per course, per programme, plus overview |
| `handbook.py` | Handbook → one chunk per numbered (sub)section, tables intact |
| `parse.py` | Routes a filename to its chunker; refuses unknown documents |
| `chunks.py` | The `Chunk` type, and the context line every chunk opens with |
| `embed.py` | OpenAI `text-embedding-3-small`, one entry point for docs and queries |
| `pipeline.py` | Re-runnable ingest: parse → embed → replace, keyed by filename |

The two documents get different chunkers on purpose — one generic chunker
measurably hurts one of them. Neither has a chunk size: both cut on structure the
documents already declare (course entries; numbered sections).

## Running it

```bash
# Chunk boundaries only -- no database, no API key, no network.
docker compose exec backend python scripts/inspect_chunks.py --summary
docker compose exec backend python scripts/inspect_chunks.py --ref 2.3

# Full ingestion. Needs OPENAI_API_KEY.
docker compose exec backend python scripts/ingest_documents.py
docker compose exec backend python scripts/ingest_documents.py --force  # after a chunker change

# Phase 3 exit check: the six-question retrieval test set.
docker compose exec backend python scripts/verify_phase3.py
```

`--force` matters after editing a chunker: the PDF is unchanged, so the default
sha256 skip would correctly decide there is nothing to do, and wrongly leave the
old chunks in place.
