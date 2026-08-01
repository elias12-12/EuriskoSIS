"""Print every chunk the ingestion pipeline would produce, without embedding it.

    docker compose exec backend python scripts/inspect_chunks.py
    docker compose exec backend python scripts/inspect_chunks.py --summary
    docker compose exec backend python scripts/inspect_chunks.py --ref 2.3

Needs neither the database nor an API key, which is the point: chunk boundaries
are the one Phase 3 decision that cannot be checked by a later test. A wrong
boundary does not raise -- it produces a passage that retrieves well and answers
badly -- so it gets read by eye, once, in full. PROJECT_PLAN Phase 3's exit check
tests retrieval; this tests what retrieval is choosing between.

It also runs the chunkers' own assertions (33 courses, every course keeping its
prerequisite line, the Handbook's full section list), so a failure here is the
fast way to see that a document no longer matches the parser.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import get_settings
from ingestion.chunks import Chunk
from ingestion.parse import CHUNKERS, parse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--summary", action="store_true", help="one line per chunk, no bodies"
    )
    parser.add_argument(
        "--ref", help="show only chunks with this section_ref, e.g. 2.3 or CENG 320"
    )
    args = parser.parse_args()

    data_dir = get_settings().data_dir
    total = 0

    for filename in CHUNKERS:
        path = data_dir / filename
        if not path.exists():
            print(f"MISSING: {path}", file=sys.stderr)
            return 1

        document, chunks = parse(path)
        selected = [c for c in chunks if args.ref is None or c.section_ref == args.ref]
        total += len(chunks)

        print(f"\n{'#' * 78}")
        print(f"# {document.title}  ({document.filename})")
        print(f"# {document.page_count} pages -> {len(chunks)} chunks")
        print(f"{'#' * 78}")

        for index, chunk in enumerate(selected):
            if args.summary:
                print(f"  {index:>3}  {_summarise(chunk)}")
            else:
                _show(index, chunk)

    print(f"\nTotal: {total} chunks across {len(CHUNKERS)} documents")
    return 0


def _summarise(chunk: Chunk) -> str:
    first_line = chunk.content.splitlines()[1] if "\n" in chunk.content else ""
    return (
        f"p{chunk.page:<2} {chunk.chunk_kind:<8} "
        f"{str(chunk.section_ref or '-'):<10} "
        f"{len(chunk.content):>5} chars  {first_line[:52]}"
    )


def _show(index: int, chunk: Chunk) -> None:
    print(f"\n{'-' * 78}")
    print(
        f"[{index}] kind={chunk.chunk_kind} ref={chunk.section_ref} "
        f"page={chunk.page} chars={len(chunk.content)}"
    )
    print(f"     cite as: {chunk.citation()}")
    print(f"{'-' * 78}")
    print(chunk.content)


if __name__ == "__main__":
    raise SystemExit(main())
