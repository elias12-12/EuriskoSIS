"""The unit of retrieval, and the one rule both chunkers share.

A chunk is not just a slice of text: CLAUDE.md section 7 rule 5 requires every
document-based answer to cite its document and section, so a chunk that cannot
say where it came from cannot be used no matter how well it matches a query.
`section_ref` and `page` are therefore part of the type, not optional metadata
bolted on afterwards.

The shared rule is that **the heading is inside the content, not only beside it**.
"Prerequisite: MECH 210" embeds to something almost meaningless on its own; with
"MECH 310 Fluid Mechanics" above it, the same chunk answers "what do I need
before Fluid Mechanics?". Every chunk therefore opens with a context line naming
the document and section, which costs a few tokens and buys the retrieval most of
its precision.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    """One retrievable passage, ready to embed and store."""

    content: str
    chunk_kind: str
    section_ref: str | None
    section_title: str | None
    page: int

    def citation(self) -> str:
        """How this chunk names itself in an answer."""
        if self.section_ref and self.section_title:
            return f"section {self.section_ref} ({self.section_title}), page {self.page}"
        if self.section_ref:
            return f"section {self.section_ref}, page {self.page}"
        if self.section_title:
            return f"{self.section_title}, page {self.page}"
        return f"page {self.page}"


def build(
    *,
    document_title: str,
    context: str,
    body: list[str],
    chunk_kind: str,
    section_ref: str | None,
    section_title: str | None,
    page: int,
) -> Chunk:
    """Assemble a chunk, prefixing the context line described above.

    `context` is the human path to this passage -- "Section 2.3", "Course
    description", "Programme requirements" -- and is what makes two chunks with
    similar wording distinguishable to the embedding model.
    """
    heading = f"{document_title} - {context}"
    return Chunk(
        content="\n".join([heading, *body]).strip(),
        chunk_kind=chunk_kind,
        section_ref=section_ref,
        section_title=section_title,
        page=page,
    )
