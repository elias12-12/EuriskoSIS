"""Chunker for the Undergraduate Course Catalogue.

The Catalogue is a directory, not an essay: two pages of requirement tables
followed by 33 short, rigidly formatted course entries. Its natural retrieval
unit is therefore the *entry*, and the chunker cuts on the document's own
structure rather than on a token budget.

Three chunk shapes come out of it:

- **one chunk per course** (33), each holding the code-and-title line, the
  description, and the prerequisite line. PROJECT_PLAN Phase 3 is explicit that a
  course must never be split from its prerequisites, and this is why: "What are
  the prerequisites for CENG 320?" is answerable only by a passage containing
  both the code and the prereq line. A fixed-size chunker would routinely put the
  prerequisite line of one course in the same window as the title of the next,
  which is the exact failure mode that produces a confident wrong answer.
- **one chunk per programme** (2), each holding that programme's five
  requirement categories with their credits and course lists.
- **overview chunks** for the narrative front matter, including "How degree
  requirements work" -- which carries both the per-category credit totals and the
  rule that surplus credits in one category do not offset a shortfall in another.

Course entries are additionally prefixed with their subject heading ("Computer
Engineering (CENG)"), because the subject is what disambiguates a query phrased
by topic rather than by code.
"""

from __future__ import annotations

import re

from ingestion.chunks import Chunk, build
from ingestion.extract import ExtractedDocument, Line

# "ENGR 450   Introduction to Artificial Intelligence   (3 credits)"
_COURSE = re.compile(r"^([A-Z]{4} \d{3})\s+(.+?)\s+\((\d+) credits?\)$")
# "Computer Engineering (BE-CENG)" -- checked before _SUBJECT, which it also matches.
_PROGRAMME = re.compile(r"^(.+?)\s+\((BE-[A-Z]+)\)$")
# "Engineering (ENGR)", "Computer Science (CMPS)"
_SUBJECT = re.compile(r"^(.+?)\s+\(([A-Z]{4})\)$")

# The Catalogue's narrative headings, in document order. Hardcoded *and*
# asserted: unlike course entries these have no structural marker to derive them
# from, so the chunker states what it expects and refuses to run if the document
# has changed underneath it. Same discipline as the loader's derive-then-verify
# of `selection_rule` -- guessing at a heading would silently reshape every chunk
# boundary after it.
_NARRATIVE_HEADINGS = (
    "The Bachelor of Engineering",
    "How degree requirements work",
    "Programmes",
    "Course Descriptions",
)
# A pure container: it introduces the two programme headings and owns no text.
_CONTAINER_HEADINGS = frozenset({"Programmes"})

EXPECTED_COURSE_COUNT = 33  # stated on the Catalogue's own cover page


def chunk_catalogue(document: ExtractedDocument) -> list[Chunk]:
    """Split the Catalogue into course, programme and overview chunks."""
    _assert_headings_present(document)

    chunks: list[Chunk] = []
    pending: list[Line] = []
    # What the lines currently being accumulated belong to. Starts as the title
    # block, which is real document content and is kept rather than dropped.
    current: dict[str, object] = {
        "kind": "overview",
        "ref": None,
        "title": "Front matter",
        "context": "Front matter",
    }
    subject = ""

    def flush() -> None:
        if not pending:
            return
        chunks.append(
            build(
                document_title=document.title,
                context=str(current["context"]),
                body=[line.text for line in pending],
                chunk_kind=str(current["kind"]),
                section_ref=current["ref"],  # type: ignore[arg-type]
                section_title=current["title"],  # type: ignore[arg-type]
                page=pending[0].page,
            )
        )
        pending.clear()

    for line in document.lines:
        text = line.text

        if text in _CONTAINER_HEADINGS:
            # Owns no text of its own; the programme headings that follow do.
            continue

        if text in _NARRATIVE_HEADINGS:
            flush()
            current = {
                "kind": "overview",
                "ref": None,
                "title": text,
                "context": text,
            }
            pending.append(line)
            continue

        if programme := _PROGRAMME.match(text):
            flush()
            name, code = programme.group(1), programme.group(2)
            current = {
                "kind": "program",
                "ref": code,
                "title": name,
                "context": f"Programme requirements, {name} ({code})",
            }
            pending.append(line)
            continue

        if subject_match := _SUBJECT.match(text):
            # A heading, not content: it labels the courses beneath it, and is
            # replayed into each of their chunks instead of forming one of its own.
            flush()
            subject = text
            current = {
                "kind": "overview",
                "ref": None,
                "title": subject_match.group(1),
                "context": f"Course descriptions, {text}",
            }
            continue

        if course := _COURSE.match(text):
            flush()
            code, title = course.group(1), course.group(2)
            current = {
                "kind": "course",
                "ref": code,
                "title": title,
                "context": f"Course description, {subject}" if subject else "Course description",
            }
            pending.append(line)
            continue

        pending.append(line)

    flush()

    courses = [chunk for chunk in chunks if chunk.chunk_kind == "course"]
    if len(courses) != EXPECTED_COURSE_COUNT:
        raise ValueError(
            f"{document.filename}: chunked {len(courses)} courses, "
            f"expected {EXPECTED_COURSE_COUNT}"
        )
    _assert_prerequisites_attached(courses)
    return chunks


def _assert_headings_present(document: ExtractedDocument) -> None:
    seen = {line.text for line in document.lines}
    missing = [heading for heading in _NARRATIVE_HEADINGS if heading not in seen]
    if missing:
        raise ValueError(
            f"{document.filename}: expected headings not found: {missing} -- "
            "the Catalogue's structure has changed and the chunker needs revisiting"
        )


def _assert_prerequisites_attached(courses: list[Chunk]) -> None:
    """Every course entry ends with its prerequisite line, or ingestion fails.

    This is the invariant the whole chunker exists to protect. A course chunk
    that lost its prerequisites still looks plausible -- it has a code, a title
    and a description -- and would answer "what are the prerequisites for X?"
    with silence or, worse, with the neighbouring course's line.
    """
    orphans = [
        chunk.section_ref
        for chunk in courses
        if not re.search(r"^Prerequisites?: .+$", chunk.content, re.MULTILINE)
    ]
    if orphans:
        raise ValueError(
            f"course chunks with no prerequisite line: {orphans} -- "
            "a course must never be separated from its prerequisites"
        )
