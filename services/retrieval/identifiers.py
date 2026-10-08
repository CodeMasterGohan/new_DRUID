"""Identifier-like token extraction for the literal-match path (§10 step 7).

BM25 gives lexical relevance, not literal matching, so identifier lookups
(invoice numbers, part numbers, clause references, order IDs, UUIDs) get
their own authorized branch against the ``identifiers`` array field (§9).
"""

from __future__ import annotations

import re

# Ordered by specificity; first match wins per span. Kept intentionally
# narrow — false positives here only cost a cheap extra branch query.
_PATTERNS: tuple[re.Pattern, ...] = (
    # UUIDs
    re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    ),
    # Clause / section references: "§ 4.2", "clause 7", "section 3.1.4"
    re.compile(r"§\s?\d+(?:\.\d+)*"),
    re.compile(r"\b(?:clause|section|article)\s+\d+(?:\.\d+)*\b", re.IGNORECASE),
    # Prefixed codes: "INV-2024-001", "PO 88123", "SN ABC123"
    re.compile(r"\b[A-Z]{2,}[-_ ]\d[\w-]*\b"),
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
    for pattern in _PATTERNS:
        for match in pattern.finditer(query):
            token = match.group(0).strip()
            key = token.lower()
            if key not in seen:
                seen.add(key)
                found.append(token)
    return found
