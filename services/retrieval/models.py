"""Data models: stored records, scored hits, and the §10 result contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Provenance = Literal["native-detected", "olmocr-routed"]
Modality = Literal["text", "page_image"]

TRUST_MARKER = "untrusted-document-content"


@dataclass(frozen=True)
class StoredRecord:
    """One Milvus record as returned by the vector store.

    Mirrors the §9 schema: ``dense_vector``/``sparse_vector`` are omitted
    (never leave the store), ``metadata`` carries ``modality`` and any
    provenance detail.
    """

    chunk_id: str
    document_id: str
    document_version: str
    filename: str
    page_start: int
    page_end: int
    heading_path: tuple[str, ...] = ()
    text: str = ""
    provenance: Provenance = "native-detected"
    modality: Modality = "text"
    page_image_ref: str | None = None  # object-storage key; null for text
    source_reference: str = ""
    identifiers: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ScoredHit:
    """A stored record with its 1-based rank inside one retrieval branch."""

    record: StoredRecord
    rank: int  # 1-based rank within the branch that produced this hit
    score: float | None = None  # branch-native score, if the store provides one


@dataclass(frozen=True)
class FusedCandidate:
    """A deduplicated candidate with its RRF fusion score."""

    record: StoredRecord
    fusion_score: float
    branches: tuple[str, ...] = ()  # which branches contributed, e.g. ("dense", "bm25")


@dataclass
class SearchResult:
    """The §10 result contract, exactly.

    ``reranker_score`` may be None (page_image records are never reranked).
    Scores are raw model outputs — never present them as calibrated
    probabilities.
    """

    document_id: str
    document_version: str
    chunk_id: str
    filename: str
    page_start: int
    page_end: int
    heading_path: list[str]
    text: str
    rank: int
    reranker_score: float | None
    source_reference: str
    provenance: Provenance
    modality: Modality
    page_image_ref: str | None
    trust: str = TRUST_MARKER

    def to_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "document_version": self.document_version,
            "chunk_id": self.chunk_id,
            "filename": self.filename,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "heading_path": list(self.heading_path),
            "text": self.text,
            "rank": self.rank,
            "reranker_score": self.reranker_score,
            "source_reference": self.source_reference,
            "provenance": self.provenance,
            "modality": self.modality,
            "page_image_ref": self.page_image_ref,
            "trust": self.trust,
        }
