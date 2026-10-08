# Retrieval service — hybrid search + RRF + MiniLM rerank (spec §10)

CPU-only FastAPI service (port 8004). Implements the §10 search sequence
exactly; see `service.py`.

## Layout

| File | Responsibility |
|---|---|
| `service.py` | `RetrievalService.search(context, query)` — the 12-step sequence; the §4 tenant-aware repository entry point |
| `config.py` | Tunable targets: dense 50, lexical 50, identifier 20, rerank 50, final 8, RRF k=60, identifier boost, `max_page_image_results=2` (all env-overridable) |
| `interfaces.py` | Injectable boundaries: `EmbeddingClient`, `VectorStore`, `CatalogSearchPort`, `Reranker` |
| `predicates.py` | `AuthPredicate` — one object built per search, passed identically to dense/BM25/identifier branches; renders to a Milvus filter expr |
| `prompts.py` | EmbeddingGemma 2 contractual query prompts (§8): `task: question answering` default, `task: code retrieval` for code queries (heuristic + `task_hint` override), `task: search result` alternative |
| `identifiers.py` | Identifier-like token extraction (invoice/part/clause/UUID patterns) gating the literal-match branch |
| `fusion.py` | RRF (`1/(k+rank)`) dedupe by `chunk_id`; identifier hits merged with a fixed additive boost and never lost to dilution |
| `reranker.py` | Cross-encoder scoring of TEXT records only; page_image records bypass and re-enter by fusion score; input-budget truncation guard |
| `citations.py` | Metadata-only `source_reference`; `wrap_passage` provenance delimiters + `trust: untrusted-document-content`; citation assembly never touches raw text |
| `catalog_port.py` | `SqlAlchemyCatalogPort` — uses `CatalogRepository.searchable_versions_query`, never reimplements the filter. Mapping contract: Milvus `document_id == str(Document.id)`, `document_version == str(DocumentVersion.version_number)` — ingestion must write exactly these encodings |
| `context.py` | Fail-closed context validation |
| `main.py` | HTTP entrypoint: verifies the gateway's HMAC-signed `X-Druid-*` identity headers, then delegates to the injected service |

## Key behaviors

- **Fail closed:** missing/invalid context raises `ContextValidationError`; no context → no data, no default tenant.
- **Catalog hard filter:** `allowed_versions` is fetched before retrieval AND re-checked after fusion (race safety); empty set short-circuits to `[]` without touching the vector store.
- **Embedding contract:** the query vector width is validated against the configured 768 (Matryoshka width if validated); mismatch raises `DependencyError` rather than mixing contracts.
- **Modality mix:** reranked text first, then up to `max_page_image_results` page-image records by fusion score; tune on the M1 corpus.
- **Neighbor expansion** (step 10) is opt-in and authorized with the same predicate; expansions append after `final_results` with continuing ranks.

## Tests

`pytest services/retrieval/tests/` — 25 tests: RRF correctness, identifier
boost, identical predicates on all branches, fail-closed matrix, page_image
rerank bypass, exact result-contract fields, modality cap, version
reconciliation race, task prompts, embedding width check, citation hygiene.
