# ingestion

PDF parsing, chunking, and embedding scripts — callable by the backend so the
admin panel's "re-run ingestion" button has something real to invoke. Built in
Phase 3 (see [PROJECT_PLAN.md](../PROJECT_PLAN.md)).

The Catalogue and the Handbook get different chunking strategies on purpose; one
generic chunker measurably hurts one of the two. Reasoning goes in
[DESIGN.md](../DESIGN.md) as it's decided.

Directory exists from Phase 0 so the repo layout is fixed from day one.
