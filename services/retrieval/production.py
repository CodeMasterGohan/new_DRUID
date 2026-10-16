"""Production retrieval adapters (no model downloads and no implicit tenant).

All Milvus branches consume the exact same catalog-backed AuthPredicate.
Never widen the scope after a backend error, and never return unscoped data.
"""
from __future__ import annotations

import json
import os
from typing import Sequence

import httpx

from .interfaces import EmbeddingClient, Reranker, VectorStore
from .models import ScoredHit, StoredRecord
from .predicates import AuthPredicate


class HttpEmbeddingClient(EmbeddingClient):
    """Bridge the legacy prompted-text retrieval interface to /embed.

    Embedding service owns the real query prefix. Strip only the three known
    prefixes, then send raw query plus task, preventing double-prefixing.
    """

    _PREFIXES = {
        "task: question answering | query: ": "question_answering",
        "task: code retrieval | query: ": "code_retrieval",
        "task: search result | query: ": "search_result",
    }

    def __init__(self, url: str, dimension: int = 768, timeout: float = 30.0):
        if not url or dimension < 1:
            raise ValueError("embedding URL and dimension are required")
        self._url = url.rstrip("/") + "/embed"
        self._dimension = dimension
        self._timeout = timeout

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_query(self, prompted_text: str) -> list[float]:
        prefix = next((p for p in self._PREFIXES if prompted_text.startswith(p)), None)
        if prefix is None:
            raise ValueError("unexpected query prefix; refusing to embed with wrong task")
        query = prompted_text[len(prefix):]
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(self._url, json={
                "queries": [query], "task": self._PREFIXES[prefix],
            })
            response.raise_for_status()
            data = response.json()
        vectors = data.get("vectors", [])
        if data.get("dimension") != self.dimension or len(vectors) != 1:
            raise ValueError("embedding endpoint violated vector dimension/count contract")
        vector = vectors[0]
        if not isinstance(vector, list) or len(vector) != self.dimension:
            raise ValueError("embedding response has invalid width")
        return [float(v) for v in vector]


class MilvusVectorStore(VectorStore):
    """Milvus v2.5 client, preserving tenant AND active-version predicates."""

    FIELDS = (
        "chunk_id", "document_id", "document_version", "text",
        "page_start", "page_end", "identifiers", "metadata", "chunk_order",
    )

    def __init__(self, uri: str, collection: str, client=None):
        if not uri or not collection:
            raise ValueError("Milvus URI and collection are required")
        if client is None:
            from pymilvus import MilvusClient
            client = MilvusClient(uri=uri, token=os.getenv("MILVUS_TOKEN") or "") if os.getenv("MILVUS_TOKEN") else MilvusClient(uri=uri)
        self._client = client
        self._collection = collection

    @staticmethod
    def _record(row: dict) -> StoredRecord:
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        if not isinstance(metadata, dict):
            raise ValueError("invalid Milvus metadata")
        modality = metadata.get("modality", "text")
        if modality not in ("text", "page_image"):
            raise ValueError(f"invalid retrieval modality {modality!r}")
        provenance = metadata.get("detection_provenance") or "native-detected"
        if provenance not in ("native-detected", "olmocr-routed"):
            provenance = "native-detected"
        return StoredRecord(
            chunk_id=str(row["chunk_id"]),
            document_id=str(row["document_id"]),
            document_version=str(row["document_version"]),
            filename=str(metadata.get("filename") or "document"),
            page_start=int(row.get("page_start") or 1),
            page_end=int(row.get("page_end") or row.get("page_start") or 1),
            heading_path=tuple(metadata.get("heading_path") or ()),
            text=str(row.get("text") or ""),
            provenance=provenance,
            modality=modality,
            page_image_ref=metadata.get("render_ref"),
            source_reference=str(metadata.get("source_reference") or ""),
            identifiers=tuple(row.get("identifiers") or ()),
            metadata=metadata,
        )

    def _search(self, data, anns_field: str, predicate: AuthPredicate, top_k: int, params: dict) -> list[ScoredHit]:
        expr = predicate.to_milvus_expr()
        response = self._client.search(
            collection_name=self._collection, data=[data], anns_field=anns_field,
            filter=expr, limit=max(1, top_k), output_fields=list(self.FIELDS),
            search_params=params,
        )
        hits = response[0] if response else []
        return [
            ScoredHit(record=self._record(hit.get("entity") or hit),
                      rank=i, score=float(hit.get("distance", 0)))
            for i, hit in enumerate(hits, 1)
        ]

    def dense_search(self, vector: Sequence[float], predicate: AuthPredicate, top_k: int) -> list[ScoredHit]:
        return self._search(list(vector), "dense_vector", predicate, top_k,
                            {"metric_type": "COSINE", "params": {"ef": 64}})

    def bm25_search(self, query_text: str, predicate: AuthPredicate, top_k: int) -> list[ScoredHit]:
        return self._search(query_text, "sparse_vector", predicate, top_k,
                            {"metric_type": "BM25", "params": {}})

    def identifier_search(self, identifiers: Sequence[str], predicate: AuthPredicate, top_k: int) -> list[ScoredHit]:
        tokens = list(dict.fromkeys(t for t in identifiers if t))[:32]
        if not tokens:
            return []
        expr = predicate.to_milvus_expr()
        expr += " and ARRAY_CONTAINS_ANY(identifiers, " + json.dumps(tokens) + ")"
        rows = self._client.query(
            collection_name=self._collection, filter=expr,
            output_fields=list(self.FIELDS), limit=max(1, top_k),
        )
        return [ScoredHit(record=self._record(row), rank=i) for i, row in enumerate(rows, 1)]

    def fetch_neighbors(self, record: StoredRecord, predicate: AuthPredicate, window: int = 1) -> list[StoredRecord]:
        if window <= 0:
            return []
        # Requires the source ordinal, which is preserved in metadata by _record.
        ordinal = record.metadata.get("chunk_order")
        if ordinal is None:
            return []
        from .predicates import _escape_milvus_string
        expr = predicate.to_milvus_expr()
        expr += (
            f' and document_id == "{_escape_milvus_string(record.document_id)}"'
            f' and document_version == "{_escape_milvus_string(record.document_version)}"'
            f" and chunk_order >= {max(0, int(ordinal) - window)}"
            f" and chunk_order <= {int(ordinal) + window}"
        )
        rows = self._client.query(
            collection_name=self._collection, filter=expr,
            output_fields=list(self.FIELDS), limit=2 * window + 1,
        )
        return [self._record(row) for row in rows if str(row.get("chunk_id")) != record.chunk_id]


class MiniLMReranker(Reranker):
    """CPU-only cross encoder, loaded exclusively from pre-staged weights."""

    def __init__(self, model_path: str):
        if not model_path or not os.path.isdir(model_path):
            raise RuntimeError("MINILM_MODEL_PATH must be a pre-staged local directory")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from sentence_transformers import CrossEncoder
        self._model = CrossEncoder(model_path, device="cpu", trust_remote_code=False)

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        return [float(v) for v in self._model.predict([(query, p) for p in passages])]
