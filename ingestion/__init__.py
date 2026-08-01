"""Document ingestion: parse, chunk, embed, store.

Lives at the repo root (the Phase 0 layout) but is copied into the backend image
and importable as `ingestion`, because the admin panel's "re-run ingestion"
button calls `pipeline.ingest_all` in-process rather than shelling out.

The two source PDFs get two different chunkers on purpose -- see the module
docstrings in `catalogue.py` and `handbook.py` for the reasoning, and DESIGN.md
for the summary.
"""
