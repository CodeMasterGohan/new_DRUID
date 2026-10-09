# Concepts glossary

Short definitions for terms that come up when using the two systems.

## Identity & tenancy (new_DRUID)

- **Tenant** — the isolation boundary. Every document, chunk, job, cache key,
  and quota is scoped to exactly one tenant.
- **`group_id`** — the tenant identifier, carried as a JWT claim. It comes
  from the validated token only — never from tool arguments, headers, file
  names, or document content.
- **Roles** — `user` (default; own tenancy only), `member` (authenticated API
  role), `reviewer` (may resolve quarantined review items), `admin` (operator;
  cross-tenant views). `admin` is accepted everywhere a narrower role is and
  is never grantable through the UI.
- **Fail closed** — missing/invalid auth, tenant context, or artifacts produce
  an explicit failure, never an unrestricted fallback.

## Ingestion states (new_DRUID)

`UPLOADED → QUEUED → PARSING → CHUNKING → EMBEDDING → INDEXING → READY`.
Exceptional: `RETRY_PENDING` (transient, retrying), `FAILED` (see Quarantine),
`REVIEW_REQUIRED` (human decision needed; unreviewed pages stay unsearchable),
`DELETING`, `DELETED` (every store confirmed). Only `READY` is fully searchable.

## Retrieval pipeline (new_DRUID)

- **Hybrid retrieval** — one query runs three authorized branches: dense
  vectors (EmbeddingGemma 2, 768-dim, HNSW, cosine), **BM25** lexical sparse
  search, and exact **identifier** match (invoice numbers, part numbers —
  BM25 alone doesn't guarantee literal matching).
- **RRF** — reciprocal rank fusion: merges the ranked lists from the branches.
- **Rerank width** — top-50 candidates re-scored locally by the MiniLM
  cross-encoder (CPU); final answer set is 8 passages.
- **Page router** — each PDF page is independently routed: native pdftext
  extraction, or a page render → olmOCR 2 FP8 (visual path). Low-quality output
  goes to retry or the review queue, never silently into the index.
- **Table-region detection** — native-path tables are detected from line
  geometry and handed to pdftext's table extractor; uncertain tables route the
  page to olmOCR, which reconstructs them natively.
- **Page-image records** — page renders embedded with the vision encoder into
  the same 768-dim space as text (`modality: "page_image"`); text queries can
  retrieve images without image input.
- **Provenance delimiters** — every passage is wrapped in
  `<<<druid:passage …>>>` / `<<<druid:end-passage>>>` (New_sage uses
  `<<<sage:passage>>>`) carrying library/filename, version, page range or
  section, and `trust="untrusted-document-content"`.
- **Untrusted document content** — retrieved text is data, never instructions;
  it must not alter identity, tenant scope, permissions, or behavior.

## Index & data (new_DRUID)

- **Collection / alias** — Milvus physical collection `project_knowledge_base_v1`
  behind the stable alias `project_knowledge_base`; tenant routing via the
  `group_id` partition key (application-layer repository is the real auth
  boundary, not partition behavior).
- **Immutable revisions** — `library_version` / document-version rows are
  immutable once indexed; re-crawls create new rows, exactly one active per
  (library, version). Old rows stay for reproducibility until explicitly pruned.
- **Version activation** — a new version is indexed, its artifact/chunk counts
  checked against the plausible range for the parser revision, then activated;
  retrieval hard-filters inactive/deleting/unreviewed versions in the catalog
  layer regardless of Milvus compaction state.
- **Artifact manifest** — per approved model/dependency: upstream source,
  immutable revision, sha256 checksums, license, approval record. Required
  before staging.

## New_sage specifics

- **Chunking 420/48** — docs split on headings into ~420-token chunks with
  48-token overlap (tokenizer-aware; code blocks never split mid-block). Sized
  so passage + query stays inside MiniLM's 512-token window.
- **Stale-while-revalidate** — a query for a stale (library, version) serves
  the existing index immediately and triggers a background re-crawl; never
  blocks on the crawl.
- **Version fallback** — an unindexed version serves the closest indexed one
  with `version_fallback: true` and a `status` note, instead of failing.
- **Repo-clone pinning** — when a `repo_url` is known, the version is pinned
  by `git clone --branch <tag>` and `README.md` + `docs/**/*.md` are ingested
  directly (no HTML extraction); web crawling is the fallback.
- **Weekly refresh sweep + monthly DevDocs sync** — automated jobs; DevDocs
  libraries are re-synced from the monthly DevDocs image (diff versions,
  re-ingest only what changed), never re-crawled from upstream sites.

## Operations (both)

- **Offline-first** — production services load pre-staged artifacts from
  read-only mounts, `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`, and fail
  startup rather than download anything at runtime.
- **GPU arbitration** — exactly one GPU consumer on the H200: vLLM serving
  olmOCR-2-7B-FP8 with an explicit memory cap (initial 0.7). Embeddings and
  the reranker run on CPU. Any additional GPU consumer requires re-validation.
- **Quotas (new_DRUID)** — per tenant: 1,000 ingestion pages/day, 10 query
  QPS, 200 MB max upload, 2,000 max pages/document. Exhaustion → 429 with
  `Retry-After`.
- **Matryoshka width** — EmbeddingGemma 2's 768-dim vectors may be truncated to
  512/256/128 (with re-normalization) after M1 validation, applied identically
  at index and query time; any embedding-contract change requires full
  re-indexing.
- **M1 eval corpus** — the milestone gate: 50–100 representative documents
  with labeled Q/A pairs that router thresholds, chunking, and retrieval
  parameters are tuned against. Not yet built at last build log.
