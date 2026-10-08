"""Injectable collaborator interfaces.

The Milvus schema code, the real EmbeddingGemma 2 weights, the MiniLM
checkpoint, and the catalog's SQLAlchemy session are all sibling or
deployment concerns. This service codes against these interfaces only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from services.common.request_context import RequestContext

from .models import ScoredHit, StoredRecord
from .predicates import AuthPredicate


class EmbeddingClient(ABC):
    """CPU embedding service boundary (EmbeddingGemma 2, §8)."""

    @abstractmethod
    def embed_query(self, prompted_text: str) -> list[float]:
        """Embed one task-prompted query string. Never downloads at runtime."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Expected vector width (768 default; Matryoshka width if validated)."""


class VectorStore(ABC):
    """Milvus access boundary (§9). Every method takes the SAME predicate."""

    @abstractmethod
    def dense_search(
        self, vector: Sequence[float], predicate: AuthPredicate, top_k: int
    ) -> list[ScoredHit]:
        """Single ANN search over the unified 768-dim space (text + page_image)."""

    @abstractmethod
    def bm25_search(
        self, query_text: str, predicate: AuthPredicate, top_k: int
    ) -> list[ScoredHit]:
        """Lexical retrieval over the analyzer-enabled text field."""

    @abstractmethod
    def identifier_search(
        self, identifiers: Sequence[str], predicate: AuthPredicate, top_k: int
    ) -> list[ScoredHit]:
        """Literal match against the ``identifiers`` array field."""

    def fetch_neighbors(
        self, record: StoredRecord, predicate: AuthPredicate, window: int = 1
    ) -> list[StoredRecord]:
        """Authorized parent/neighbor expansion (step 10). Default: no-op."""
        return []


class CatalogSearchPort(ABC):
    """Catalog hard-filter boundary (§10 step 3, §12).

    Implementations MUST use the catalog's ``searchable_versions_query``
    (active + READY + unreviewed-clean + non-deleted document, tenant-scoped)
    and MUST NOT reimplement the filter.
    """

    @abstractmethod
    def allowed_versions(
        self, context: RequestContext
    ) -> frozenset[tuple[str, str]]:
        """Return ``(document_id, document_version)`` string keys searchable
        for this tenant right now. Empty set means: return [] immediately."""


class Reranker(ABC):
    """Local CPU cross-encoder boundary (MiniLM, §10 step 9)."""

    model_id: str = "cross-encoder/ms-marco-MiniLM-L6-v2"

    @abstractmethod
    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """Score query/passage pairs. Raw scores only — never calibrated
        probabilities. Called with TEXT passages exclusively."""
