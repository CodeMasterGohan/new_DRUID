"""RetrievalService: the §10 search sequence, exactly.

``repository.search(context, query)`` is the tenant-aware entry point (§4).
One :class:`AuthPredicate` is built per search and passed identically to
the dense, BM25, and identifier branches.
"""

from __future__ import annotations

import logging
from typing import Any

from services.common.request_context import RequestContext

from .citations import build_source_reference
from .config import RetrievalConfig
from .context import validate_context
from .errors import DependencyError
from .fusion import apply_identifier_boost, reciprocal_rank_fusion
from .identifiers import extract_identifier_tokens
from .interfaces import (
    CatalogSearchPort,
    EmbeddingClient,
    Reranker,
    VectorStore,
)
from .models import FusedCandidate, ScoredHit, SearchResult, TRUST_MARKER
from .predicates import AuthPredicate
from .prompts import build_query_prompt, detect_task
from .reranker import rerank_text_candidates

log = logging.getLogger(__name__)


class RetrievalService:
    """Tenant-aware hybrid retrieval (§10). All collaborators are injected."""

    def __init__(
        self,
        *,
        embedding: EmbeddingClient,
        vector_store: VectorStore,
        catalog: CatalogSearchPort,
        reranker: Reranker,
        config: RetrievalConfig | None = None,
    ) -> None:
        self._embedding = embedding
        self._store = vector_store
        self._catalog = catalog
        self._reranker = reranker
        self._config = config or RetrievalConfig()

    # ------------------------------------------------------------------
    # §4 tenant-aware repository interface
    # ------------------------------------------------------------------
    def search(
        self,
        context: Any,
        query: str,
        *,
        task_hint: str | None = None,
        expand_neighbors: bool | None = None,
    ) -> list[SearchResult]:
        """Execute the §10 search sequence. Fails closed on bad context."""
        cfg = self._config

        # 1. Validate caller context — missing/invalid fails closed.
        ctx = validate_context(context)

        # 2-3. Tenant predicate + catalog hard filter (never index state).
        allowed = self._catalog.allowed_versions(ctx)
        if not allowed:
            log.info("no searchable versions for tenant", extra={"tenant": ctx.group_id})
            return []
        predicate = AuthPredicate(group_id=ctx.group_id, allowed_versions=allowed)

        # 4. Query embedding with the task-appropriate prompt (§8).
        task = detect_task(query, task_hint)
        prompted = build_query_prompt(query, task)
        try:
            query_vector = self._embedding.embed_query(prompted)
        except Exception as exc:
            raise DependencyError(f"query embedding failed: {exc}") from exc
        if (
            len(query_vector) != cfg.embedding_dimension
            or self._embedding.dimension != cfg.embedding_dimension
        ):
            raise DependencyError(
                f"embedding width {len(query_vector)} (client declares "
                f"{self._embedding.dimension}) != contract "
                f"{cfg.embedding_dimension}; refusing to mix contracts (§8)"
            )

        # 5-7. Authorized dense / BM25 / identifier retrieval — SAME predicate.
        try:
            dense_hits = self._store.dense_search(
                query_vector, predicate, cfg.dense_candidates
            )
            bm25_hits = self._store.bm25_search(
                query, predicate, cfg.lexical_candidates
            )
            id_tokens = extract_identifier_tokens(query)
            identifier_hits = (
                self._store.identifier_search(
                    id_tokens, predicate, cfg.identifier_candidates
                )
                if id_tokens
                else []
            )
        except Exception as exc:
            raise DependencyError(f"vector store query failed: {exc}") from exc

        # 8. Deduplicate + RRF fusion; identifier hits merged with fixed boost.
        fused = reciprocal_rank_fusion(
            {"dense": dense_hits, "bm25": bm25_hits}, k=cfg.rrf_k
        )
        fused = apply_identifier_boost(fused, identifier_hits, cfg.identifier_boost)
        shortlist = fused[: cfg.rerank_candidates]

        # 9. Rerank TEXT records only (CPU). Page-image records bypass the
        # cross-encoder and re-enter by fusion score.
        reranked = rerank_text_candidates(query, shortlist, self._reranker)
        image_records = sorted(
            (c for c in shortlist if c.record.modality == "page_image"),
            key=lambda c: c.fusion_score,
            reverse=True,
        )[: cfg.max_page_image_results]

        ordered: list[tuple[FusedCandidate, float | None]] = [
            (cand, score) for cand, score in reranked
        ] + [(cand, None) for cand in image_records]
        finalists = ordered[: cfg.final_results]

        results = [
            self._to_result(ctx, cand, reranker_score, rank)
            for rank, (cand, reranker_score) in enumerate(finalists, start=1)
        ]

        # 10. Optional authorized parent/neighbor expansion (same predicate).
        do_expand = cfg.expand_neighbors if expand_neighbors is None else expand_neighbors
        if do_expand:
            results = self._expand_neighbors(ctx, predicate, results, cfg)

        # 11. Reconcile with catalog state (race safety: a version may have
        # been deactivated between step 3 and now).
        fresh_allowed = self._catalog.allowed_versions(ctx)
        results = [
            r
            for r in results
            if (r.document_id, r.document_version) in fresh_allowed
        ]
        # Re-number ranks after reconciliation filtering.
        for rank, r in enumerate(results, start=1):
            r.rank = rank

        # 12. Provenance-tagged results per the §10 contract.
        return results

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _to_result(
        self,
        ctx: RequestContext,
        candidate: FusedCandidate,
        reranker_score: float | None,
        rank: int,
    ) -> SearchResult:
        rec = candidate.record
        return SearchResult(
            document_id=rec.document_id,
            document_version=rec.document_version,
            chunk_id=rec.chunk_id,
            filename=rec.filename,
            page_start=rec.page_start,
            page_end=rec.page_end,
            heading_path=list(rec.heading_path),
            text=rec.text,
            rank=rank,
            reranker_score=reranker_score,
            source_reference=rec.source_reference
            or build_source_reference(
                rec.filename, rec.page_start, rec.page_end, list(rec.heading_path)
            ),
            provenance=rec.provenance,
            modality=rec.modality,
            page_image_ref=rec.page_image_ref,
            trust=TRUST_MARKER,
        )

    def _expand_neighbors(
        self,
        ctx: RequestContext,
        predicate: AuthPredicate,
        results: list[SearchResult],
        cfg: RetrievalConfig,
    ) -> list[SearchResult]:
        """Fetch authorized neighboring records and append them after the
        final results (ranks continue past ``final_results``)."""
        expanded = list(results)
        seen = {r.chunk_id for r in results}
        for result in results:
            # Reconstruct a minimal record handle for the neighbor lookup.
            from .models import StoredRecord

            handle = StoredRecord(
                chunk_id=result.chunk_id,
                document_id=result.document_id,
                document_version=result.document_version,
                filename=result.filename,
                page_start=result.page_start,
                page_end=result.page_end,
            )
            try:
                neighbors = self._store.fetch_neighbors(
                    handle, predicate, cfg.neighbor_window
                )
            except Exception as exc:
                raise DependencyError(f"neighbor expansion failed: {exc}") from exc
            for neighbor in neighbors:
                if neighbor.chunk_id in seen:
                    continue
                if (neighbor.document_id, neighbor.document_version) not in (
                    predicate.allowed_versions or set()
                ):
                    continue
                seen.add(neighbor.chunk_id)
                expanded.append(
                    self._to_result(
                        ctx,
                        FusedCandidate(record=neighbor, fusion_score=0.0),
                        None,
                        len(expanded) + 1,
                    )
                )
        return expanded
