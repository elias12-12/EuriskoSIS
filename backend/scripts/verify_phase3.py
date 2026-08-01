"""Phase 3 exit check: does retrieval find the right section, with no agent involved?

    docker compose exec backend python scripts/ingest_documents.py
    docker compose exec backend python scripts/verify_phase3.py
    docker compose exec backend python scripts/verify_phase3.py --show   # print hits

Needs OPENAI_API_KEY: a query has to be embedded by the same model as the corpus.

The six questions are the fixed set from PROJECT_PLAN Phase 3, and the assertion
is the one that phase's exit check states -- **the top retrieved chunk must
contain the answer**, not merely be on a related topic. So each case names the
section that must come back first, and a substring that must appear in it.

Naming the expected section is the point. "Retrieval returned something about
grading" is not a passing result when the question was how the GPA is
calculated: section 1.1 is the scale, 1.2 is the formula, and only one of them
answers it. Asserting the exact `section_ref` is what makes the difference
visible instead of leaving it to a reading of the output.

Deliberately no agent, no model call beyond the embedding. If retrieval is wrong
here it will be wrong behind the agent too, and much harder to see.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from app.db import SessionLocal
from app.retrieval import RetrievedChunk, corpus_status, search
from ingestion.embed import MissingAPIKey
from ingestion.parse import CATALOGUE_FILENAME, HANDBOOK_FILENAME


@dataclass(frozen=True)
class Case:
    question: str
    document: str
    # None where more than one section legitimately answers the question; see
    # _GEN_ED_ACCEPTABLE_REFS.
    section_ref: str | None
    # Text that must appear in the top hit. This is the "contains the answer"
    # half of the exit check -- a chunk can be from the right section and still
    # have been split so that the answer itself fell into the neighbour.
    must_contain: str
    why: str


CASES = (
    Case(
        question="What are the prerequisites for CENG 320?",
        document=CATALOGUE_FILENAME,
        section_ref="CENG 320",
        must_contain="Prerequisite: CENG 310",
        why=(
            "The case the per-course chunker exists for. A fixed-window chunker "
            "puts one course's prerequisite line next to the following course's "
            "title, and the answer comes back confidently wrong."
        ),
    ),
    Case(
        question="How many credits do I need in General Education?",
        document=CATALOGUE_FILENAME,
        section_ref=None,
        must_contain="9 credits",
        why=(
            "Answerable from the requirements overview or from either "
            "programme's table -- all three state 9 credits -- so this asserts "
            "the credit figure rather than one section. Asserting the phrase "
            "'General Education' instead would pass on a chunk that mentions "
            "the category without giving its credit total."
        ),
    ),
    Case(
        question="When is the last day to drop a course without a W?",
        document=HANDBOOK_FILENAME,
        section_ref="5",
        must_contain="25 September 2026",
        why=(
            "The calendar, not the add/drop policy in 2.3: 2.3 gives the rule "
            "('end of the third week'), section 5 gives the date. Both are "
            "relevant, only one is an answer."
        ),
    ),
    Case(
        question="What happens if I fail a required course?",
        document=HANDBOOK_FILENAME,
        section_ref="1.5",
        must_contain="must be repeated",
        why="Repeating a course, not the grading scale that defines F.",
    ),
    Case(
        question="How is my GPA calculated?",
        document=HANDBOOK_FILENAME,
        section_ref="1.2",
        must_contain="divided by the total credits attempted",
        why=(
            "1.2 is the formula; 1.1 is the scale. Retrieving 1.1 would look "
            "plausible and would not answer the question."
        ),
    ),
    Case(
        question="Who do I contact about a scholarship?",
        document=HANDBOOK_FILENAME,
        section_ref="9",
        must_contain="finance@eurisko.edu",
        why=(
            "The routing table. This is the chunk that must never be split "
            "mid-row -- an office with no enquiry attached answers nothing."
        ),
    ),
)

# General Education credits appear in three places; any of them answers it.
_GEN_ED_ACCEPTABLE_REFS = {None, "BE-CENG", "BE-MECH"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--show", action="store_true", help="print the full text of each top hit"
    )
    parser.add_argument(
        "--top-k", type=int, default=3, help="how many hits to display per question"
    )
    args = parser.parse_args()

    with SessionLocal() as session:
        if (problem := _check_corpus(session)) is not None:
            print(problem, file=sys.stderr)
            return 2

        failures = 0
        for case in CASES:
            try:
                hits = search(session, case.question, top_k=args.top_k)
            except MissingAPIKey as exc:
                print(f"\n{exc}", file=sys.stderr)
                return 2

            failures += _report(case, hits, show=args.show)

    print()
    if failures:
        print(f"FAILED: {failures} of {len(CASES)} questions retrieved the wrong chunk.")
        print("Fix retrieval before touching the agent (PROJECT_PLAN Phase 3).")
        return 1
    print(f"PASSED: all {len(CASES)} questions retrieved a top chunk containing the answer.")
    return 0


def _check_corpus(session) -> str | None:
    """Fail early and specifically if nothing has been ingested.

    Every question failing at once almost always means an empty corpus, not bad
    retrieval, and that is a wasted debugging session waiting to happen.
    """
    rows = corpus_status(session)
    if not rows:
        return (
            "No documents ingested. Run:\n"
            "  docker compose exec backend python scripts/ingest_documents.py"
        )
    unready = [row for row in rows if row["status"] != "ready"]
    if unready:
        details = ", ".join(
            f"{row['filename']} ({row['status']}: {row['error']})" for row in unready
        )
        return f"Documents not ready: {details}"
    unembedded = [row for row in rows if row["chunk_count"] != row["embedded_count"]]
    if unembedded:
        return f"Documents with unembedded chunks: {[r['filename'] for r in unembedded]}"
    return None


def _report(case: Case, hits: list[RetrievedChunk], *, show: bool) -> int:
    """Print one case's result; return 1 if it failed."""
    print(f"\n{'=' * 78}")
    print(f"Q: {case.question}")

    if not hits:
        print("   FAIL  no hits at all")
        return 1

    top = hits[0]
    section_ok = (
        top.section_ref in _GEN_ED_ACCEPTABLE_REFS
        if case.section_ref is None
        else top.section_ref == case.section_ref
    )
    document_ok = top.document_filename == case.document
    content_ok = case.must_contain.lower() in top.content.lower()
    passed = section_ok and document_ok and content_ok

    print(f"   top: {top.citation()}   (similarity {top.similarity:.3f})")
    print(f"   {'PASS' if passed else 'FAIL'}", end="")
    if passed:
        print(f"  contains {case.must_contain!r}")
    else:
        reasons = []
        if not document_ok:
            reasons.append(f"expected {case.document}")
        if not section_ok:
            expected = case.section_ref or f"one of {sorted(map(str, _GEN_ED_ACCEPTABLE_REFS))}"
            reasons.append(f"expected section {expected}, got {top.section_ref}")
        if not content_ok:
            reasons.append(f"top chunk does not contain {case.must_contain!r}")
        print(f"  {'; '.join(reasons)}")
        print(f"   why this matters: {case.why}")

    for rank, hit in enumerate(hits[1:], start=2):
        print(f"    {rank}. {hit.citation()}   ({hit.similarity:.3f})")

    if show:
        print(f"\n{'-' * 78}\n{top.content}\n{'-' * 78}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
