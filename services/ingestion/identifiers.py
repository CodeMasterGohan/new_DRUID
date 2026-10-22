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
# avoid substring noise. Kept aligned with services.retrieval.identifiers so
# ingested identifiers and query-time identifier tokens match symmetrically.
DEFAULT_PATTERNS: dict[str, str] = {
    # UUIDs
    "uuid": (
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    ),
    # Honda-style part numbers (23441-HM8-B60) and dashed numeric codes
    "part_number": r"\b(?:\d{5}-[A-Z0-9]{3}-[A-Z0-9]{3,4}|\d[\w]*-\w+-\w+)\b",
    # Invoice / order numbers: INV-2024-00123, PO#45821, Order 99213
    "invoice_number": r"\b(?:INV|PO|ORDER)[-#\s]*[A-Z0-9][A-Z0-9\-/]{2,20}\b",
    # Clause / section / article references: § 4.2, Section 12.3(a), clause 7
    "clause_ref": (
        r"(?:§\s*\d+(?:\.\d+)*(?:\([a-z]\))?"
        r"|\b(?:Section|Clause|Article)\s+\d+(?:\.\d+)*(?:\([a-z]\))?)"
    ),
    # Multi-segment document numbers: DOC-ABC-1234, CVE-2024-12345
    "document_number": r"\b[A-Z]{2,6}-[A-Z0-9]{2,8}-\d{2,8}\b",
    # Prefixed alphanumeric / error codes: AB-1234, ERR_404, SN-9912
    "prefixed_code": r"\b[A-Z]{2,8}[-_]\d[\w.-]*\b",
    # Version / release tags: v1.2.3, v1.2.3-rc1
    "version_tag": r"\bv\d+\.\d+(?:\.\d+)*(?:-[A-Za-z0-9.]+)?\b",
    # Long digit runs (serial / account / order numbers)
    "serial_digits": r"\b\d{6,}\b",
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
        # Collect (start, -length, value) across all patterns so longer spans
        # at the same start offset win over shorter sub-matches.
        hits: list[tuple[int, int, str]] = []
        for pattern in self._compiled.values():
            for match in pattern.finditer(text):
                value = match.group(0).strip()
                hits.append((match.start(), -len(value), value))
        hits.sort()
        last_end = -1
        for start, neg_len, value in hits:
            end = start - neg_len
            if start < last_end:
                continue
            last_end = end
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
