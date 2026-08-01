"""Run the document ingestion pipeline.

    docker compose exec backend python scripts/ingest_documents.py
    docker compose exec backend python scripts/ingest_documents.py --force

Needs OPENAI_API_KEY (embeddings are OpenAI text-embedding-3-small, CLAUDE.md
section 3). To check chunk boundaries without a key or a network, use
`scripts/inspect_chunks.py` instead -- it runs the same parsers and stops short
of embedding.

`--force` re-embeds documents whose bytes are unchanged. That is the flag to use
after editing a chunker: the PDF is identical, so the default skip would
correctly decide there is nothing to do, and wrongly leave the old chunks in place.
"""

from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import SessionLocal
from ingestion.embed import MissingAPIKey
from ingestion.pipeline import ingest_all


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-embed even if the file is unchanged (use after editing a chunker)",
    )
    args = parser.parse_args()

    data_dir = get_settings().data_dir
    print(f"Ingesting from {data_dir}")

    with SessionLocal() as session:
        try:
            results = ingest_all(session, data_dir, force=args.force)
        except MissingAPIKey as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 2

    failures = 0
    for result in results:
        if result.status == "ready":
            note = " (unchanged, skipped)" if result.unchanged else ""
            print(
                f"  OK      {result.filename}: {result.chunk_count} chunks "
                f"from {result.page_count} pages{note}"
            )
        else:
            failures += 1
            print(f"  FAILED  {result.filename}: {result.error}", file=sys.stderr)

    print(
        f"\n{len(results) - failures}/{len(results)} documents ready."
        if failures
        else f"\nAll {len(results)} documents ready."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
