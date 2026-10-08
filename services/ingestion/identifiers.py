"""Identifier extraction for the Milvus ``identifiers`` field (spec §9, §10).

BM25 is lexical relevance, not literal matching. This module extracts
exact-match identifiers (invoice numbers, part numbers, clause references)
during canonical-tree construction, using *configurable* per-tenant
patterns. The retrieval layer matches these literally.

Patterns are provisional defaults; tenants extend them via
``IdentifierExtractor(patterns={...})``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# Provisional default patterns (name → regex). Word-boundary anchored to
# avoid substring noise.
DEFAULT_PATTERNS: dict[str, str] = {
    # Honda-style part numbers: 23441-HM8-B60
    "part_number": r"\b\d{5}-[A-Z0-9]{3}-[A-Z0-9]{3,4}\b",
    # Invoice / order numbers: INV-2024-00123, PO#45821, Order 99213
    "invoice_number": r"\b(?:INV|PO|ORDER)[-#\s]*[A-Z0-9][A-Z0-9\-/]{3,20}\b",
    # Clause / section references: § 4.2, Section 12.3(a)
    "clause_ref": r"(?:§\s*\d+(?:\.\d+)*(?:\([a-z]\))?|\bSection\s+\d+(?:\.\d+)*(?:\([a-z]\))?)",
    # Document numbers: DOC-ABC-1234
    "document_number": r"\b[A-Z]{2,6}-[A-Z0-9]{2,8}-\d{2,8}\b",
}


@dataclass
class IdentifierExtractor:
    """Extract identifiers from chunk text with configurable patterns."""

    patterns: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_PATTERNS)
    )
    max_identifiers_per_chunk: int = 32

    def __post_init__(self) -> None:
        self._compiled = {
            name: re.compile(pattern, re.IGNORECASE)
            for name, pattern in self.patterns.items()
        }

    def extract(self, text: str) -> list[str]:
        """Return deduplicated identifiers in order of first appearance."""
        found: list[str] = []
        seen: set[str] = set()
        # Collect (position, value) across all patterns, then sort.
        hits: list[tuple[int, str]] = []
        for pattern in self._compiled.values():
            for match in pattern.finditer(text):
                value = match.group(0).strip()
                hits.append((match.start(), value))
        hits.sort()
        for _, value in hits:
            key = value.upper()
            if key not in seen:
                seen.add(key)
                found.append(value)
            if len(found) >= self.max_identifiers_per_chunk:
                break
        return found


def extract_identifiers(
    text: str, patterns: dict[str, str] | None = None
) -> list[str]:
    """Convenience wrapper with default (or overridden) patterns."""
    extractor = IdentifierExtractor(patterns=dict(patterns) if patterns else None) \
        if patterns else IdentifierExtractor()
    return extractor.extract(text)
