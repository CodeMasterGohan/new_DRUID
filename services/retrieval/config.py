"""Tunable retrieval parameters (spec §10).

All values are starting points to be tuned against the M1 eval corpus.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class RetrievalConfig:
    """Retrieval targets and fusion/rerank tuning knobs (§10)."""

    dense_candidates: int = 50
    lexical_candidates: int = 50
    identifier_candidates: int = 20
    rerank_candidates: int = 50
    final_results: int = 8
    # Page-image records are not reranked; cap how many may appear in the
    # final list. Modality mix is tuned on the M1 corpus (§8).
    max_page_image_results: int = 2
    # RRF damping constant: score = sum(1 / (k + rank)).
    rrf_k: int = 60
    # Fixed additive boost applied to identifier-match hits during fusion.
    identifier_boost: float = 1.0
    # Optional neighbor expansion (step 10), off by default.
    expand_neighbors: bool = False
    neighbor_window: int = 1
    # Expected embedding width; the query vector is validated against it.
    embedding_dimension: int = 768

    @classmethod
    def from_env(cls) -> "RetrievalConfig":
        return cls(
            dense_candidates=_int("RETRIEVAL_DENSE_CANDIDATES", 50),
            lexical_candidates=_int("RETRIEVAL_LEXICAL_CANDIDATES", 50),
            identifier_candidates=_int("RETRIEVAL_IDENTIFIER_CANDIDATES", 20),
            rerank_candidates=_int("RETRIEVAL_RERANK_CANDIDATES", 50),
            final_results=_int("RETRIEVAL_FINAL_RESULTS", 8),
            max_page_image_results=_int("RETRIEVAL_MAX_PAGE_IMAGE_RESULTS", 2),
            rrf_k=_int("RETRIEVAL_RRF_K", 60),
            identifier_boost=_float("RETRIEVAL_IDENTIFIER_BOOST", 1.0),
            expand_neighbors=os.environ.get("RETRIEVAL_EXPAND_NEIGHBORS", "").lower()
            in ("1", "true", "yes"),
            neighbor_window=_int("RETRIEVAL_NEIGHBOR_WINDOW", 1),
            embedding_dimension=_int("EMBEDDING_DIMENSION", 768),
        )
