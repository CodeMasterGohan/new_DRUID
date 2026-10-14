"""Citation assembly and untrusted-content wrapping (§10).

Retrieved text is untrusted content. It must never be interpolated into
system-adjacent fields, and every passage handed to a calling agent is
wrapped with explicit provenance delimiters plus the trust marker so the
agent can distinguish data from instructions.
"""

from __future__ import annotations

from .models import TRUST_MARKER, SearchResult

_BEGIN = "[BEGIN RETRIEVED PASSAGE"
_END = "[END RETRIEVED PASSAGE]"


def build_source_reference(
    filename: str, page_start: int, page_end: int, heading_path: list[str]
) -> str:
    """Build a human-readable citation from metadata only.

    Takes metadata fields — never raw document text — so there is no path
    for document content to leak into system-adjacent fields.
    """
    if page_start == page_end:
        pages = f"p. {page_start}"
    else:
        pages = f"pp. {page_start}-{page_end}"
    ref = f"{filename} ({pages})"
    if heading_path:
        ref += " — " + " / ".join(heading_path)
    return ref


def wrap_passage(result: SearchResult) -> str:
    """Wrap a result's passage with provenance delimiters + trust marker."""
    header = (
        f"{_BEGIN} — trust: {result.trust} — "
        f"source: {result.source_reference} — "
        f"provenance: {result.provenance} — "
        f"modality: {result.modality} — "
        f"chunk: {result.chunk_id}]"
    )
    safe_text = result.text.replace(_END, "\\[END RETRIEVED PASSAGE\\]")
    return f"{header}\n{safe_text}\n{_END}"


def assemble_citation(result: SearchResult) -> dict:
    """Citation metadata for a result — identifiers only, no document text."""
    return {
        "document_id": result.document_id,
        "document_version": result.document_version,
        "chunk_id": result.chunk_id,
        "source_reference": result.source_reference,
        "page_start": result.page_start,
        "page_end": result.page_end,
        "rank": result.rank,
        "provenance": result.provenance,
        "modality": result.modality,
        "trust": TRUST_MARKER,
    }
