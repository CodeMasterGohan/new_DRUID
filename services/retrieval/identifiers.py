"""Identifier-like token extraction for the literal-match path (§10 step 7).

BM25 gives lexical relevance, not literal matching, so identifier lookups
(invoice numbers, part numbers, clause references, order IDs, UUIDs) get
their own authorized branch against the ``identifiers`` array field (§9).
"""

from __future__ import annotations

import re

# Ordered by specificity; first match wins per span. Kept intentionally
# aligned with services.ingestion.identifiers.DEFAULT_PATTERNS so query tokens
# match the identifiers extracted at ingestion time.
_PATTERNS: tuple[re.Pattern, ...] = (
    # UUIDs
    re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    ),
    # Clause / section / article references: "§ 4.2", "Section 12.3(a)", "clause 7"
    re.compile(r"§\s*\d+(?:\.\d+)*(?:\([a-z]\))?", re.IGNORECASE),
    re.compile(
        r"\b(?:clause|section|article)\s+\d+(?:\.\d+)*(?:\([a-z]\))?",
        re.IGNORECASE,
    ),
    # Invoice / PO / order numbers: "INV-2024-001", "PO#45821", "Order 99213"
    re.compile(
        r"\b(?:INV|PO|ORDER)[-#\s]*[A-Za-z0-9][A-Za-z0-9\-/]{2,20}\b",
        re.IGNORECASE,
    ),
    # Multi-segment document numbers: "DOC-ABC-1234", "CVE-2024-12345"
    re.compile(r"\b[A-Za-z]{2,6}-[A-Za-z0-9]{2,8}-\d{2,8}\b"),
    # Prefixed codes: "AB-1234", "ERR_404", "SN ABC123"
    re.compile(r"\b(?:[A-Za-z]{2,8}[-_]\d[\w.-]*|[A-Z]{2,}[-_ ]\d[\w-]*)\b"),
    # Version / release tags: "v1.2.3", "v1.2.3-rc1"
    re.compile(r"\bv\d+\.\d+(?:\.\d+)*(?:-[A-Za-z0-9.]+)?\b"),
    # Dashed numeric codes: "23441-HM8-B60"
    re.compile(r"\b\d[\w]*-\w+-\w+\b"),
    # Long digit runs that are unlikely to be prose: order/serial numbers
    re.compile(r"\b\d{6,}\b"),
)


def extract_identifier_tokens(query: str) -> list[str]:
    """Return deduplicated identifier-like tokens from ``query``.

    Returns [] when the query carries no identifier-like tokens — the
    caller then skips the identifier branch entirely.
    """
    found: list[str] = []
    seen: set[str] = set()
    hits: list[tuple[int, int, str]] = []
    for pattern in _PATTERNS:
        for match in pattern.finditer(query):
            token = match.group(0).strip()
            hits.append((match.start(), -len(token), token))
    hits.sort()
    last_end = -1
    for start, neg_len, token in hits:
        end = start - neg_len
        if start < last_end:
            continue
        last_end = end
        key = token.lower()
        if key not in seen:
            seen.add(key)
            found.append(token)
    return found
