"""RetrievalService: the §10 search sequence, exactly.

``repository.search(context, query)`` is the tenant-aware entry point (§4).
One :class:`AuthPredicate` is built per search and passed identically to
the dense, BM25, and identifier branches.
"""

from __future__ import annotations

import logging
import time
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
        diagnostics: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Execute the §10 search sequence. Fails closed on bad context."""
        cfg = self._config
        started = time.monotonic()

        # 1. Validate caller context — missing/invalid fails closed.
        ctx = validate_context(context)
        log.info(
            "search start: group=%s request_id=%s query_chars=%d",
            ctx.group_id,
            ctx.request_id,
            len(query),
        )

        # 2-3. Tenant predicate + catalog hard filter (never index state).
        allowed = self._catalog.allowed_versions(ctx)
        if not allowed:
            log.info("no searchable versions for tenant", extra={"tenant": ctx.group_id})
            if diagnostics is not None:
                diagnostics.update({"authorized_versions": 0, "result_count": 0,
                                    "branches": {"dense": 0, "bm25": 0, "identifier": 0}})
            return []
        predicate = AuthPredicate(group_id=ctx.group_id, allowed_versions=allowed)
        catalog_done = time.monotonic()

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

        embed_done = time.monotonic()
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

        candidates_done = time.monotonic()
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
        if image_records:
            # Fallback path: page-image records carry no rerankable text, so
            # they bypass the cross-encoder and re-enter by fusion score.
            log.info(
                "reranker bypass: %d page-image record(s) re-enter by fusion "
                "score (request_id=%s)",
                len(image_records),
                ctx.request_id,
            )

        # Reserve a bounded slot for a relevant page image when both
        # modalities are available. The previous text-first truncation made
        # page images unreachable whenever >= final_results text hits existed.
        image_slots = (
            0 if reranked and cfg.final_results == 1
            else min(
                len(image_records), cfg.max_page_image_results,
                max(1, cfg.final_results // 4)
            ) if reranked
            else min(len(image_records), cfg.final_results)
        )
        text_slots = cfg.final_results - image_slots
        rerank_done = time.monotonic()
        ordered: list[tuple[FusedCandidate, float | None]] = [
            (cand, score) for cand, score in reranked[:text_slots]
        ] + [(cand, None) for cand in image_records[:image_slots]]
        finalists = ordered

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
        before_reconcile = len(results)
        results = [
            r
            for r in results
            if (r.document_id, r.document_version) in fresh_allowed
        ]
        if len(results) != before_reconcile:
            log.info(
                "reconciliation dropped %d result(s) deactivated mid-search "
                "(request_id=%s)",
                before_reconcile - len(results),
                ctx.request_id,
            )
        # Re-number ranks after reconciliation filtering.
        for rank, r in enumerate(results, start=1):
            r.rank = rank

        elapsed_ms = (time.monotonic() - started) * 1000.0
        log.info(
            "search finish: %d results in %.1fms group=%s request_id=%s",
            len(results),
            elapsed_ms,
            ctx.group_id,
            ctx.request_id,
        )
        if diagnostics is not None:
            # Authorized callers only; never emit raw documents or query text.
            final_done = time.monotonic()
            diagnostics.update({
                "authorized_versions": len(allowed),
                "branches": {
                    "dense": len(dense_hits),
                    "bm25": len(bm25_hits),
                    "identifier": len(identifier_hits),
                },
                "fused_count": len(fused),
                "reranked_text_count": len(reranked),
                "page_image_candidates": len(image_records),
                "dropped_after_catalog_recheck": before_reconcile - len(results),
                "result_count": len(results),
                "timings_ms": {
                    "catalog": round((catalog_done - started) * 1000, 2),
                    "embedding": round((embed_done - catalog_done) * 1000, 2),
                    "candidate_search": round((candidates_done - embed_done) * 1000, 2),
                    "fusion_rerank": round((rerank_done - candidates_done) * 1000, 2),
                    "reconciliation": round((final_done - rerank_done) * 1000, 2),
                    "total": round((final_done - started) * 1000, 2),
                },
                "ranked_chunk_ids": [r.chunk_id for r in results[:20]],
            })
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
