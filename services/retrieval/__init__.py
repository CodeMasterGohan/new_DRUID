"""Retrieval service for new_DRUID (spec §10).

Implements the §10 search sequence exactly:

1. Validate caller context (fail closed).
2. Construct tenant + document-ACL predicates.
3. Exclude inactive/deleting/unreviewed versions at the catalog layer
   (hard filter — never relies on Milvus index/compaction state).
4. Generate the query embedding with the task-appropriate prompt (§8).
5. Authorized dense retrieval over the unified 768-dim space
   (text chunks AND page_image records, single ANN search).
6. Authorized BM25 retrieval.
7. Authorized literal identifier match (identifier-like tokens only).
8. Deduplicate + RRF fusion (identifier hits merged with a fixed boost).
9. Local CPU rerank of TEXT records only; page_image records enter
   fusion on dense rank only (they carry no rerankable text).
10. Optional authorized parent/neighbor expansion.
11. Reconcile candidate versions with catalog state (race safety).
12. Return provenance-tagged results per the §10 result contract.

All collaborators (embedding client, vector store, catalog port, reranker)
are injectable interfaces — see :mod:`services.retrieval.interfaces`.
"""

from .service import RetrievalService

__all__ = ["RetrievalService"]
