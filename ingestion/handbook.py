"""Chunker for the Student Handbook.

The Handbook is the opposite of the Catalogue: dense prose and tables under a
numbered hierarchy (1, 1.1 ... 9). That numbering is already a human-authored
statement of where one idea ends and the next begins, so the chunker follows it
rather than imposing a window of its own -- and it gives citations for free,
because "Student Handbook section 2.2" is exactly how a policy should be quoted.

Cut points:

- A **subsection** heading (`1.1`, `2.3`, `6.2`) starts a new chunk.
- A **section** heading (`3.`, `5.`, `9.`) starts a new chunk holding whatever
  text sits between it and its first subsection. For sections with no
  subsections -- 3 Graduation, 5 Academic Calendar, 7 Financial Assistance,
  8 Student Services, 9 Where to Take a Question -- that is the whole section.
- Section 1 has no lead-in text at all, so no chunk is emitted for the bare
  heading. Empty chunks are never produced.

Two consequences worth stating, because both were choices:

**Tables are never split.** Every table in this document lives entirely inside
one subsection, so following the numbering keeps the grading scale (1.1), the
add/drop deadlines (2.3), the calendar (5), the fee table (6), the awards table
(7), the services directory (8) and the routing table (9) each intact. Splitting
the routing table mid-row would leave a chunk naming an office with no enquiry
attached to it, which is worse than not retrieving it at all.

**Section 5 stays one chunk containing both term calendars.** Fall 2026 and
Spring 2027 could be split into two, which would sharpen retrieval slightly.
They are not, because "the last day to drop a course without a W" has a
different answer in each term, and a chunk holding only one of them invites a
confidently wrong answer about the other. One chunk holding both forces the
answer to name the term.
"""

from __future__ import annotations

import re

from ingestion.chunks import Chunk, build
from ingestion.extract import ExtractedDocument, Line

# "1. Grading and Academic Standing"
_SECTION = re.compile(r"^(\d+)\.\s+(\S.*)$")
# "2.3 Adding, dropping and withdrawing"
_SUBSECTION = re.compile(r"^(\d+\.\d+)\s+(\S.*)$")

# The Handbook's own numbering, from its structure. Asserted after chunking so a
# silently dropped section fails ingestion instead of producing an assistant that
# answers "I don't know" about a policy that is right there in the document.
EXPECTED_SECTION_REFS = (
    "1.1", "1.2", "1.3", "1.4", "1.5",
    "2.1", "2.2", "2.3",
    "3",
    "4", "4.1",
    "5",
    "6", "6.1", "6.2",
    "7",
    "8",
    "9",
)


def chunk_handbook(document: ExtractedDocument) -> list[Chunk]:
    """Split the Handbook into one chunk per numbered (sub)section."""
    chunks: list[Chunk] = []
    pending: list[Line] = []
    section_heading = ""
    current: dict[str, object] = {
        "kind": "overview",
        "ref": None,
        "title": "Front matter",
        "context": "Front matter",
    }

    def flush() -> None:
        if not pending:
            return
        body = [line.text for line in pending]
        # The parent section heading is replayed above a subsection's own
        # heading, so "2.3 Adding, dropping and withdrawing" carries "2.
        # Registration" with it. Without it, a subsection heading like "1.5
        # Repeating a course" embeds without the word "grading" anywhere near it.
        if section_heading and body[0] != section_heading:
            body = [section_heading, *body]
        chunks.append(
            build(
                document_title=document.title,
                context=str(current["context"]),
                body=body,
                chunk_kind=str(current["kind"]),
                section_ref=current["ref"],  # type: ignore[arg-type]
                section_title=current["title"],  # type: ignore[arg-type]
                page=pending[0].page,
            )
        )
        pending.clear()

    for line in document.lines:
        if subsection := _SUBSECTION.match(line.text):
            flush()
            ref, title = subsection.group(1), subsection.group(2)
            current = {
                "kind": "policy",
                "ref": ref,
                "title": title,
                "context": f"Section {ref}, {title}",
            }
            pending.append(line)
            continue

        if section := _SECTION.match(line.text):
            flush()
            ref, title = section.group(1), section.group(2)
            section_heading = line.text
            current = {
                "kind": "policy",
                "ref": ref,
                "title": title,
                "context": f"Section {ref}, {title}",
            }
            # Deliberately not appended: `flush` replays `section_heading` at the
            # top of the body, so appending here would duplicate it. A section
            # whose next line is a subsection therefore flushes nothing, which is
            # how section 1 correctly produces no chunk of its own.
            continue

        pending.append(line)

    flush()
    _assert_sections_present(document, chunks)
    return chunks


def _assert_sections_present(
    document: ExtractedDocument, chunks: list[Chunk]
) -> None:
    found = tuple(
        chunk.section_ref for chunk in chunks if chunk.chunk_kind == "policy"
    )
    if found != EXPECTED_SECTION_REFS:
        raise ValueError(
            f"{document.filename}: chunked sections {found} but expected "
            f"{EXPECTED_SECTION_REFS} -- the Handbook's structure has changed"
        )
