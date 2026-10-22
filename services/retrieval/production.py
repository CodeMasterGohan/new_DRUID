"""Production retrieval adapters (no model downloads and no implicit tenant).

All Milvus branches consume the exact same catalog-backed AuthPredicate.
Never widen the scope after a backend error, and never return unscoped data.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import os
import threading
from typing import Sequence

import httpx

from .interfaces import EmbeddingClient, Reranker, VectorStore
from .models import ScoredHit, StoredRecord
from .predicates import AuthPredicate


class HttpEmbeddingClient(EmbeddingClient):
    """Bridge the legacy prompted-text retrieval interface to /embed.

    Embedding service owns the real query prefix. Strip only the three known
    prefixes, then send raw query plus task, preventing double-prefixing.
    Reuses a persistent httpx.Client for TCP connection pooling and caches
    recent query vectors in a bounded thread-safe LRU cache.
    """

    _PREFIXES = {
        "task: question answering | query: ": "question_answering",
        "task: code retrieval | query: ": "code_retrieval",
        "task: search result | query: ": "search_result",
    }

    def __init__(
        self,
        url: str,
        dimension: int = 768,
        timeout: float = 30.0,
        cache_size: int = 256,
    ):
        if not url or dimension < 1:
            raise ValueError("embedding URL and dimension are required")
        self._url = url.rstrip("/") + "/embed"
        self._dimension = dimension
        self._timeout = timeout
        self._cache_size = max(0, cache_size)
        self._cache: OrderedDict[tuple[str, str], tuple[float, ...]] = OrderedDict()
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None

    @property
    def dimension(self) -> int:
        return self._dimension

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def embed_query(self, prompted_text: str) -> list[float]:
        prefix = next((p for p in self._PREFIXES if prompted_text.startswith(p)), None)
        if prefix is None:
            raise ValueError("unexpected query prefix; refusing to embed with wrong task")
        task = self._PREFIXES[prefix]
        query = prompted_text[len(prefix):]
        cache_key = (task, query)
        if self._cache_size > 0:
            with self._lock:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    self._cache.move_to_end(cache_key)
                    return list(cached)

        client = self._get_client()
        if hasattr(client, "post"):
            response = client.post(self._url, json={
                "queries": [query], "task": task,
            })
        else:
            with client as ctx_client:
                response = ctx_client.post(self._url, json={
                    "queries": [query], "task": task,
                })
        response.raise_for_status()
        data = response.json()
        vectors = data.get("vectors", [])
        if data.get("dimension") != self.dimension or len(vectors) != 1:
            raise ValueError("embedding endpoint violated vector dimension/count contract")
        vector = vectors[0]
        if not isinstance(vector, list) or len(vector) != self.dimension:
            raise ValueError("embedding response has invalid width")
        result = [float(v) for v in vector]
        if self._cache_size > 0:
            with self._lock:
                self._cache[cache_key] = tuple(result)
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        return result


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
        metadata = {**metadata, "chunk_order": row.get("chunk_order")}
        return StoredRecord(
            chunk_id=str(row["chunk_id"]),
            document_id=str(row["document_id"]),
            document_version=str(row["document_version"]),
            filename=str(metadata.get("filename") or "document"),
            page_start=int(row["page_start"]) if row.get("page_start") is not None else 1,
            page_end=(
                int(row["page_end"]) if row.get("page_end") is not None
                else int(row["page_start"]) if row.get("page_start") is not None
                else 1
            ),
            heading_path=tuple(metadata.get("heading_path") or ()),
            text=str(row.get("text") or ""),
            provenance=provenance,
            modality=modality,
            page_image_ref=metadata.get("render_ref"),
            source_reference=str(metadata.get("source_reference") or ""),
            identifiers=tuple(row.get("identifiers") or ()),
            metadata=metadata,
        )

    def _predicates(self, predicate):
        size = int(os.environ.get("MILVUS_FILTER_BATCH_SIZE", "256"))
        if not 1 <= size <= 4096:
            raise ValueError("MILVUS_FILTER_BATCH_SIZE must be between 1 and 4096")
        if predicate.allowed_versions is None:
            yield predicate
            return
        if not predicate.allowed_versions:
            raise ValueError("empty allowed_versions")
        pairs = sorted(predicate.allowed_versions)
        for offset in range(0, len(pairs), size):
            yield AuthPredicate(predicate.group_id, frozenset(pairs[offset:offset + size]))

    def _search(self, data, anns_field: str, predicate: AuthPredicate, top_k: int, params: dict) -> list[ScoredHit]:
        # Every batch retains tenant + exact doc/version ACL filtering. Searching
        # top-k per disjoint batch then merging scores preserves global top-k.
        best = {}
        for batch in self._predicates(predicate):
            response = self._client.search(
                collection_name=self._collection, data=[data], anns_field=anns_field,
                filter=batch.to_milvus_expr(), limit=max(1, top_k),
                output_fields=list(self.FIELDS), search_params=params,
            )
            for hit in response[0] if response else []:
                record = self._record(hit.get("entity") or hit)
                score = float(hit.get("distance", 0))
                if record.chunk_id not in best or score > best[record.chunk_id][1]:
                    best[record.chunk_id] = (record, score)
            # Bound retained candidates while processing large catalogs.
            best = dict(sorted(best.items(), key=lambda item: (-item[1][1], item[0]))[:max(1, top_k)])
        return [ScoredHit(record=record, rank=i, score=score)
                for i, (record, score) in enumerate(best.values(), 1)]

    def dense_search(self, vector: Sequence[float], predicate: AuthPredicate, top_k: int) -> list[ScoredHit]:
        return self._search(list(vector), "dense_vector", predicate, top_k,
                            {"metric_type": "COSINE", "params": {"ef": 64}})

    def bm25_search(self, query_text: str, predicate: AuthPredicate, top_k: int) -> list[ScoredHit]:
        return self._search(query_text, "sparse_vector", predicate, top_k,
                            {"metric_type": "BM25", "params": {}})

    def identifier_search(self, identifiers: Sequence[str], predicate: AuthPredicate, top_k: int) -> list[ScoredHit]:
        expanded: list[str] = []
        for t in identifiers:
            if not t:
                continue
            expanded.append(t)
            upper = t.upper()
            if upper != t:
                expanded.append(upper)
        tokens = list(dict.fromkeys(expanded))[:32]
        if not tokens:
            return []
        records = {}
        for batch in self._predicates(predicate):
            expr = batch.to_milvus_expr()
            expr += " and ARRAY_CONTAINS_ANY(identifiers, " + json.dumps(tokens) + ")"
            rows = self._client.query(collection_name=self._collection, filter=expr,
                output_fields=list(self.FIELDS), limit=max(1, top_k))
            for row in rows:
                record = self._record(row)
                records[record.chunk_id] = record
            if len(records) >= max(1, top_k):
                break  # Identifier matches have no relevance score; any top-k matches suffice.
        return [ScoredHit(record=record, rank=i)
                for i, record in enumerate(list(records.values())[:max(1, top_k)], 1)]

    def fetch_neighbors(self, record: StoredRecord, predicate: AuthPredicate, window: int = 1) -> list[StoredRecord]:
        if window <= 0:
            return []
        # Requires the source ordinal, which is preserved in metadata by _record.
        ordinal = record.metadata.get("chunk_order")
        if ordinal is None:
            return []
        from .predicates import _escape_milvus_string
        pair = (record.document_id, record.document_version)
        if predicate.allowed_versions is not None and pair not in predicate.allowed_versions:
            return []
        expr = AuthPredicate(predicate.group_id, frozenset({pair})).to_milvus_expr()
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

    def __init__(self, model_path: str, batch_size: int = 32):
        if not model_path or not os.path.isdir(model_path):
            raise RuntimeError("MINILM_MODEL_PATH must be a pre-staged local directory")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from sentence_transformers import CrossEncoder
        self._model = CrossEncoder(model_path, device="cpu", trust_remote_code=False)
        self._batch_size = max(1, batch_size)

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        unique_passages = list(dict.fromkeys(passages))
        # Sort by passage length so dynamic padding within each mini-batch pads
        # to similar sequence lengths rather than the global max length.
        order = sorted(range(len(unique_passages)), key=lambda i: len(unique_passages[i]))
        sorted_pairs = [(query, unique_passages[i]) for i in order]
        raw_scores = self._model.predict(
            sorted_pairs,
            batch_size=self._batch_size,
            show_progress_bar=False,
        )
        score_by_passage: dict[str, float] = {}
        for idx, val in zip(order, raw_scores):
            score_by_passage[unique_passages[idx]] = float(val)
        return [score_by_passage[p] for p in passages]
