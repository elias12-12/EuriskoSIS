"""PDF text extraction, page-aware.

Both source PDFs are digitally generated with a text layer, so `pypdf` recovers
them cleanly -- no OCR, and the reading order is correct even inside tables.
Verified against every page of both documents before the chunkers were written.

Two decisions here shape everything downstream.

**Extraction produces a flat stream of lines carrying their page number, not a
list of per-page strings.** Handbook sections 6 (Tuition and Fees) and 8 (Student
Services) run across a page boundary mid-table. Chunking page by page would split
both, and PROJECT_PLAN Phase 3 is explicit that a table split mid-row is useless
for the "who do I contact" questions. So the chunkers see one continuous stream
and each chunk records the page its *first* line fell on.

**Running headers are removed by matching, not by dropping the first N lines.**
Every page begins with the same four-line header/footer block, which pypdf emits
before the body text. Dropping four lines blindly would silently eat real content
the day a page is laid out differently; matching them and asserting the match
fails loudly instead.

Tables arrive as one cell per line, in row-major reading order -- a routing-table
row becomes three consecutive lines (enquiry, office, contact). That is contiguous
and correct, and legible enough to embed and to answer from. It is not *structured*:
nothing here recovers cell boundaries. If a table question ever fails the Phase 3
test set, the fix is a structured table extractor (pdfplumber), not a re-chunk.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

# The running header/footer pypdf emits at the top of every page's text. Two
# lines of header ("Eurisko University" / document title) and two of footer
# ("Faculty of Engineering | ..." / "Page N"), which pypdf orders together.
_HEADER_UNIVERSITY = "Eurisko University"
_HEADER_FACULTY = re.compile(r"^Faculty of Engineering\s+\|\s+Catalogue year \d{4}-\d{4}$")
_HEADER_PAGE = re.compile(r"^Page \d+$")


@dataclass(frozen=True)
class Line:
    """One line of body text, and the page it came from."""

    text: str
    page: int


@dataclass(frozen=True)
class ExtractedDocument:
    filename: str
    title: str
    page_count: int
    sha256: str
    lines: list[Line]

    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


def extract(path: Path) -> ExtractedDocument:
    """Read a PDF into page-tagged body lines, headers stripped.

    `title` is taken from the running header rather than the filename, because it
    is what a citation should say: "Student Handbook 2026-2027" reads better in an
    answer than `Eurisko_University_Student_Handbook_2026-2027.pdf`.
    """
    raw = path.read_bytes()
    reader = PdfReader(path)

    lines: list[Line] = []
    title = ""
    for page_number, page in enumerate(reader.pages, start=1):
        page_lines = [
            stripped
            for stripped in (line.strip() for line in page.extract_text().splitlines())
            if stripped
        ]
        body, page_title = _strip_running_header(page_lines, page_number, path.name)
        title = title or page_title
        lines.extend(Line(text=text, page=page_number) for text in body)

    if not lines:
        raise ValueError(f"{path.name}: no text layer found -- this PDF needs OCR")

    return ExtractedDocument(
        filename=path.name,
        title=f"{title} {_catalogue_year(lines)}".strip(),
        page_count=len(reader.pages),
        sha256=hashlib.sha256(raw).hexdigest(),
        lines=lines,
    )


def _strip_running_header(
    page_lines: list[str], page_number: int, filename: str
) -> tuple[list[str], str]:
    """Remove the four-line running header block; return the body and the title.

    Raises if the block is not where it is expected. A header silently left in
    the body would end up embedded in a chunk and quoted back in an answer, and a
    header wrongly removed would delete real content -- both are worse than a
    failed ingestion that says which page disagreed.
    """
    if len(page_lines) < 4:
        raise ValueError(f"{filename} page {page_number}: too short to carry a header")

    university, title, faculty, page_marker = page_lines[:4]
    if (
        university != _HEADER_UNIVERSITY
        or not _HEADER_FACULTY.match(faculty)
        or not _HEADER_PAGE.match(page_marker)
    ):
        raise ValueError(
            f"{filename} page {page_number}: unexpected running header "
            f"{page_lines[:4]!r} -- extraction assumptions no longer hold"
        )
    if page_marker != f"Page {page_number}":
        raise ValueError(
            f"{filename}: header says {page_marker!r} on page {page_number}"
        )

    return page_lines[4:], title


def _catalogue_year(lines: list[Line]) -> str:
    """The academic year, for the citation title. Empty if the documents change."""
    for line in lines[:20]:
        match = re.search(r"\b(\d{4}-\d{4})\b", line.text)
        if match:
            return match.group(1)
    return ""
