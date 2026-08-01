"""Which chunker handles which document.

The routing is by filename because that is what the `documents` table is keyed
on and what the admin panel uploads. It is deliberately explicit rather than
inferred from content: a Handbook run through the Catalogue chunker would not
crash, it would produce plausible nonsense, and PROJECT_PLAN Phase 3 chose two
strategies precisely because one generic chunker measurably hurts one document.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ingestion.catalogue import chunk_catalogue
from ingestion.chunks import Chunk
from ingestion.extract import ExtractedDocument, extract
from ingestion.handbook import chunk_handbook

CATALOGUE_FILENAME = "Eurisko_University_Course_Catalogue_2026-2027.pdf"
HANDBOOK_FILENAME = "Eurisko_University_Student_Handbook_2026-2027.pdf"

CHUNKERS: dict[str, Callable[[ExtractedDocument], list[Chunk]]] = {
    CATALOGUE_FILENAME: chunk_catalogue,
    HANDBOOK_FILENAME: chunk_handbook,
}


def parse(path: Path) -> tuple[ExtractedDocument, list[Chunk]]:
    """Extract and chunk one known document.

    An unknown filename raises rather than falling back to a generic chunker.
    CLAUDE.md section 4 names the only three files the assistant may know
    anything from; silently ingesting a fourth would put ungrounded text behind
    a citation, which is the failure mode rule 1 exists to prevent.
    """
    chunker = CHUNKERS.get(path.name)
    if chunker is None:
        raise ValueError(
            f"{path.name}: no chunker registered. Known documents: "
            f"{sorted(CHUNKERS)}"
        )
    document = extract(path)
    return document, chunker(document)
