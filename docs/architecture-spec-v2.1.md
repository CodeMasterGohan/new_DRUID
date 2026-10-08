# Enterprise Local-First RAG + MCP Architecture — v2.1

Revised 2026-10-08. Supersedes v2.

## 0. Revision Notes (v1 → v2)

What changed and why:

1. **olmOCR 2 FP8 is the approved production checkpoint.** Ai2's model card recommends the FP8 variant for all practical purposes except fine-tuning, with no meaningful quality loss (82.4 vs 82.3 overall on olmOCR-bench). BF16 is retained for fine-tuning only. On a single-GPU deployment this is not a close call.
2. **Single-H200 GPU arbitration is now specified.** vLLM serving olmOCR-2-7B-FP8 owns the GPU with an explicit memory cap; the embedding service defaults to CPU (Nomic v1.5 is ~137M parameters — GPU buys almost nothing and creates contention); the MiniLM reranker runs on CPU. Ingestion bursts and query traffic no longer fight over the same card by accident.
3. **Table-region detection is specified for the native path.** pdftext's table extraction consumes table bounding boxes as input — it does not detect tables. The router now produces table-region candidates from native line geometry; low-confidence pages route to olmOCR, which handles tables natively.
4. **Page router is milestone M1 with an eval-corpus prerequisite.** Thresholds are calibrated against a labeled corpus (50–100 representative documents with Q/A pairs) built *before* the router is tuned. The `REVIEW_REQUIRED` state now has a defined quarantine surface instead of being an unexplained bucket.
5. **Chunking targets fit the reranker budget.** Prose target reduced 512 → 420 tokens (overlap 64 → 48) so passage + query stays inside MiniLM's 512-token input without truncation. `reranker_budget_enforced` is now satisfiable.
6. **Rerank funnel widened.** Rerank candidates 30 → 50; the reranker is a 22M-parameter CPU model, so this costs latency, not GPU.
7. **Exact-identifier retrieval is specified.** Added an `identifiers` keyword field and literal-match behavior — BM25 is lexical relevance, not literal matching, and the v1 spec tested for exact-identifier behavior without building the mechanism.
8. **Deletion is enforced catalog-side.** Milvus deletes go through compaction and may remain briefly visible; retrieval now hard-filters `DELETING`/`DELETED`/inactive versions in the application layer regardless of index state.
9. **Version activation uses ranges, not exact counts.** Chunk counts legitimately vary between parser revisions; activation asserts plausible ranges per parser revision.
10. **MCP responses are injection-resistant by construction.** Tool responses carry mandatory provenance tags and formatting guidance for the calling agent; "retrieved text is untrusted" is now a response-shape requirement, not just a principle.
11. **Per-tenant quotas added.** olmOCR pages are the scarcest resource; ingestion and query quotas are enforced at the gateway.
12. **Infrastructure defaults chosen.** PostgreSQL (catalog), Redis (queue broker + cache), MinIO (S3-compatible object storage). All self-hostable and offline-friendly; marked as defaults, not mandates.
13. **Backup/restore is an acceptance criterion.** A single H200 is a single point of failure; Milvus + catalog backups are load-bearing.

### Revision Notes (v2 → v2.1, 2026-10-08)

1. **Embedding model changed: Nomic Embed v1.5 → EmbeddingGemma 2** (`google/embeddinggemma-2`, Apache 2.0, released 2026-10-06). Text+image configuration (440M effective) replaces the 137M text-only model; CPU serving is retained. The unified text/image vector space enables page-render embeddings alongside text chunks (§8, §9).
2. **Task-aware prompting is now contractual.** EmbeddingGemma 2 requires asymmetric task prefixes; the vector contract (§8) pins the default prompt pair, validated on the M1 corpus.
3. **Matryoshka width is a validated option, not a default change.** 768 dims remain the default; truncation to 512/256/128 is permitted after M1 validation.

## 1. Purpose and Approved Scope

Build a self-hosted, multi-tenant document knowledge system that exposes ingestion, management, and retrieval through an authenticated API and MCP server.

The primary deployment requirement is **offline-first** operation: production services load pre-staged artifacts and communicate only with approved internal services.

**Deployment target: a single NVIDIA H200 (141 GB HBM3e).** This is a deliberate v1 constraint. It buys simplicity — no distributed scheduling, no multi-node failure modes — at the price of no failover. Section 13 makes backup/restore a first-class acceptance criterion to compensate.

### Approved model and artifact policy

- olmOCR 2 is approved; the **FP8 checkpoint is the production checkpoint** (see §5).
- Chinese-origin upstream model ancestry is not a disqualifier.
- Downloads from `modelscope.cn`, Chinese mirror repositories, or other unapproved endpoints are prohibited.
- Artifact acquisition occurs only through a controlled staging process using explicitly allowlisted sources.
- Production services must not download models, tokenizers, weights, dependencies, or other executable artifacts at runtime.
- Geographic labels are not a substitute for artifact verification.

### Architecture decisions

- Docling is excluded from the implementation.
- The native PDF extraction backend is `pdftext`, subject to corpus validation.
- PyMuPDF is an optional replacement requiring license approval.
- olmOCR 2 FP8 handles pages requiring visual reconstruction.
- EmbeddingGemma 2 (`google/embeddinggemma-2`, text+image configuration) is the embedding model, served on **CPU** by default.
- MiniLM (`cross-encoder/ms-marco-MiniLM-L6-v2`) is the initial reranker, served on **CPU**.
- Milvus provides dense and BM25 retrieval.
- Tenant authorization is enforced in the application layer, independently of Milvus partition behavior.
- vLLM serves olmOCR-2-7B-FP8 on the H200 with an explicit GPU memory cap (see §2).

`pdftext` is Apache-2.0 licensed and uses PDFium through `pypdfium2`; PyMuPDF is AGPL/commercial dual-licensed.

### Infrastructure defaults

| Concern | Default | Rationale |
|---|---|---|
| Document catalog | PostgreSQL | ACID job/version state, JSONB for flexible metadata |
| Job queue broker | Redis | Durable queue backend; also serves the retrieval cache |
| Object storage | MinIO (S3-compatible) | Self-hosted, offline-friendly, presigned-URL support |
| GPU inference | vLLM | olmOCR toolkit's recommended serving path; FP8 on Hopper |

These are defaults to unblock implementation, not mandates. Swapping any of them must preserve the contracts in §12 (durable jobs, versioned activation, complete deletion).

---

## 2. High-Level Architecture

```text
                              CLIENTS
                  +-----------------------------+
                  | Web UI | AI Agent | CLI     |
                  +--------------+--------------+
                                 |
                                 | HTTPS + access token
                                 v
                  +-----------------------------+
                  | API Gateway / Reverse Proxy |
                  | Token validation            |
                  | Rate limits / upload limits |
                  | Per-tenant ingestion quotas |
                  | Trusted identity context    |
                  +--------------+--------------+
                                 |
                    +------------+-------------+
                    |                          |
                    v                          v
          +-------------------+      +----------------------+
          | Document API      |      | MCP Server           |
          | Upload / status   |      | Streamable HTTP      |
          | List / delete     |      | Scoped tool access   |
          | Review queue      |      | Provenance-tagged    |
          +---------+---------+      | responses            |
                    |                +-----------+----------+
                    v                            |
          +-------------------+      +----------------------+
          | Document Catalog  |      | Retrieval Service    |
          | (PostgreSQL)      |      | Query embedding      |
          | Tenant ownership  |      | Dense + BM25 search  |
          | Versions / jobs   |      | Rank fusion          |
          | Searchable status |      | Local reranking      |
          +---------+---------+      | Citation assembly    |
                    |                +-----------+----------+
                    v                            |
          +-------------------+                  |
          | Durable Job Queue |                  |
          | (Redis-backed)    |                  |
          +---------+---------+                  |
                    |                            |
                    v                            |
          +----------------------------+         |
          | Ingestion Workers          |         |
          | 1. Inspect each page       |         |
          | 2. Native text extraction  |         |
          | 3. Table-region detection  |         |
          | 4. olmOCR 2 FP8 when needed|         |
          | 5. Canonical document tree |         |
          | 6. Structural chunking     |         |
          | 7. Local embeddings (CPU)  |         |
          +-------------+--------------+         |
                        |                        |
                        v                        v
                  +----------------------------------+
                  | Milvus                           |
                  | Dense vectors + generated BM25   |
                  | Tenant-filtered repository layer |
                  +----------------------------------+

          +------------------------------------------+
          | Tenant-Scoped Object Storage (MinIO)     |
          | Originals / page renders / parsed trees |
          | Markdown / provenance / retained assets |
          +------------------------------------------+

          +------------------------------------------+
          | GPU (single H200)                        |
          | vLLM: olmOCR-2-7B-FP8 (memory-capped)   |
          | CPU: embedding svc, reranker, workers    |
          +------------------------------------------+
```

### GPU arbitration policy

The H200 is shared by exactly one GPU consumer: the vLLM instance serving olmOCR 2 FP8, launched with an explicit `--gpu-memory-utilization` cap (initial: 0.70, tune against KV-cache pressure under concurrent page renders). Everything else — embedding service, reranker, ingestion workers, Milvus, API — runs on CPU/host RAM.

Rationale: EmbeddingGemma 2 (~270M text-only / ~440M text+image) and MiniLM (~22M params) gain negligible throughput from the H200 while creating contention with olmOCR's KV cache. If a future embedding model justifies GPU, it gets a *separate* time-sliced or MPS-partitioned allocation — never uncontended sharing.

### Supporting services

| Service | Responsibility |
|---|---|
| Document catalog (PostgreSQL) | Ownership, ACLs, document versions, job state, active-version tracking, review quarantine |
| Object storage (MinIO) | Original files and derived parsing artifacts |
| Job queue (Redis-backed) | Durable asynchronous ingestion and deletion work |
| Native parsing workers | CPU-based text and geometry extraction (process-based; PDFium is not thread-safe) |
| olmOCR inference service | vLLM serving olmOCR-2-7B-FP8 on the H200 |
| Embedding service | CPU-served local document and query embeddings |
| Retrieval service | Authorization, hybrid search, fusion, reranking, citations |
| Milvus | Vector and lexical indexes |
| Internal artifact registry | Approved dependencies, containers, models, and tokenizers |

---

## 3. Artifact Acquisition and Runtime Network Policy

### Controlled staging

Every approved model and dependency must have a manifest containing:

```yaml
artifact_manifest:
  artifact_name: "<model-or-package>"
  upstream_source: "<approved-source>"
  upstream_revision: "<immutable-revision>"
  internal_location: "<internal-registry-reference>"
  license: "<reviewed-license>"
  files:
    - path: "<artifact-file>"
      sha256: "<verified-checksum>"
  approval_record: "<approval-reference>"
```

The staging process must:

1. Resolve the complete artifact set, including tokenizers and configuration files.
2. Verify the final download host and redirects against the allowlist.
3. Pin immutable model revisions and dependency versions.
4. Record checksums and licenses.
5. Publish approved artifacts to the internal registry.
6. Build production images without startup package installation.
7. Test service startup with external network access blocked.

### Runtime restrictions

Production workloads:

- Load models from mounted local paths.
- Disable automatic artifact downloads.
- Do not resolve missing assets from upstream registries.
- Fail startup when required assets are missing or invalid.
- Permit only explicitly approved internal network destinations.
- Mount model artifacts read-only.

Where supported by the selected libraries:

```yaml
environment:
  HF_HUB_OFFLINE: "1"
  TRANSFORMERS_OFFLINE: "1"
```

These settings supplement network enforcement; they are not the security boundary.

---

## 4. Authentication, Authorization, and Tenant Context

### Trusted context

Every API request and MCP tool invocation receives a validated context:

```yaml
request_context:
  user_id: "<authenticated-user>"
  group_id: "<authorized-tenant>"
  roles: ["<authorized-role>"]
  request_id: "<trace-id>"
```

Tenant context must never come from:

- Document metadata.
- User-supplied tool arguments.
- Unvalidated HTTP headers.
- An MCP session ID.
- A file's original path or filename.

The gateway validates tokens, strips spoofable identity headers, and forwards identity only over an authenticated internal connection. Internal services must either validate the original token or verify the gateway's protected context.

### Tenant-scoped repository

All application access to Milvus, the catalog, object storage, and caches goes through tenant-aware interfaces:

```python
repository.search(context, query)
repository.get_document(context, document_id)
repository.list_documents(context)
repository.insert_chunks(context, chunks)
repository.delete_document(context, document_id)
```

No public interface accepts an unrestricted Milvus filter.

### Operation-specific rules

| Operation | Enforcement |
|---|---|
| Search / query / list | Mandatory tenant predicate and applicable document ACLs |
| Fetch by ID | Resolve ownership within the authenticated tenant |
| Insert | Set `group_id` from trusted context; ignore caller-supplied values |
| Upsert | Use tenant-scoped IDs and verify ownership |
| Delete | Authorize in the catalog, then execute tenant-scoped deletion |
| Hybrid search | Apply identical authorization predicates to both retrieval branches |
| Cache access | Include tenant and applicable authorization scope in the key |
| Background jobs | Persist and revalidate trusted ownership context |

Missing or invalid context fails closed.

The initial permissions model may use group-wide document access. If narrower document permissions are required, enforce them before candidates enter fusion and reranking.

### Quotas

olmOCR pages are the scarcest resource on a single GPU. The gateway enforces:

```yaml
quotas:
  ingestion_pages_per_day_per_tenant: 1000   # initial; tune against GPU headroom
  query_qps_per_tenant: 10                    # initial
  max_upload_mb: 200
  max_pages_per_document: 2000
```

Quota exhaustion returns 429 with a `Retry-After` hint, never silent degradation.

---

## 5. Document Ingestion and Page-Level Routing

### Milestone M1: eval corpus before router tuning

**Build the evaluation corpus first.** 50–100 representative documents spanning the §13 parsing-quality matrix, each with 3–10 labeled question/answer pairs (including table questions and exact-identifier lookups). Router thresholds, chunking targets, and retrieval parameters are tuned against this corpus — not against intuition. The corpus is versioned alongside the code.

### Initial supported scope

- Digital PDFs.
- Scanned PDFs.
- Mixed digital/scanned PDFs.
- Supported image files.

Other formats remain explicitly unsupported until dedicated parsers and tests are added.

### Upload workflow

1. Authenticate and authorize the upload; check tenant ingestion quota.
2. Validate file type, size, and configured page limits.
3. Generate an opaque `document_id` and a new document version.
4. Store the original under a tenant-scoped storage key.
5. Compute a content checksum.
6. Create a catalog record and durable ingestion job.
7. Return `document_id`, `job_id`, and status.

Original filenames are display metadata, not storage paths.

### Page router

Route each page independently:

```text
Native extraction (pdftext, process-based workers)
       |
       v
Text and layout quality checks
       |
       +-- Acceptable --> Table-region detection
       |                       |
       |                       +-- Tables found, high confidence
       |                       |     --> pdftext table_output with detected bboxes
       |                       |
       |                       +-- Tables uncertain/absent --> Native reconstruction
       |
       +-- Failed or complex --> Page render --> olmOCR 2 FP8
```

An intact text layer is evidence, not an automatic pass. Native extraction can fail when a PDF contains broken character mappings or problematic ordering.

Proposed routing signals:

- Extracted-text coverage and usability.
- Replacement characters or suspicious glyph mappings.
- Image-dominated content.
- Reading-order ambiguity.
- Multi-column complexity.
- Table-reconstruction quality.
- Blank-page detection.
- Comparison between page appearance and extracted content.

Thresholds are calibrated against the M1 eval corpus. **The router ships only when its page-level routing accuracy is measured** — target ≥95% agreement with human labels on the corpus before tuning retrieval around it.

### Table-region detection

pdftext extracts table *contents* from caller-supplied bounding boxes; it does not detect tables. The native path therefore includes an explicit detection step:

1. Derive candidate table regions from pdftext line geometry (aligned ruling lines, columnar whitespace gutters, repeated row pitch).
2. Score each candidate; high-confidence regions go to `pdftext.table_output`.
3. Low-confidence or geometrically ambiguous regions route the page to olmOCR, which reconstructs tables natively (84.9 on the tables split of olmOCR-bench).

Detection quality is measured on the eval corpus (table-question recall: native-detected vs olmOCR-routed). If native detection underperforms by a material margin, the default flips to routing all table-signal pages to olmOCR.

### Native path

Use `pdftext` for text and geometric extraction, followed by explicit layout and table reconstruction.

Do not assume native text extraction alone produces reliable headings, reading order, or tables.

Use process-based parsing workers because the PDFium backend is not thread-safe — this is a hard constraint, not a preference. The built-in `workers=` option (process-based) or an external process pool both satisfy it. Note: parallel extraction only engages above the per-worker page threshold; small documents run serially.

### Visual path

Use the approved production checkpoint:

```text
allenai/olmOCR-2-7B-1025-FP8
```

The BF16 checkpoint (`allenai/olmOCR-2-7B-1025`) is approved **for fine-tuning only**, not for serving.

The implementation must preserve the checkpoint's expected rendering (longest dimension 1288 px), prompt, and preprocessing behavior — use the olmOCR toolkit's builders rather than hand-rolled prompts. Low-quality or invalid output enters retry or the review queue; it never silently becomes searchable.

### Review queue

Pages or documents landing in `REVIEW_REQUIRED` appear in a quarantine surface:

- `list_documents` includes a `review_required` filter; a dedicated `get_review_item` returns the page render, both parse attempts, and routing diagnostics.
- A reviewer may accept (with a chosen parse), re-route, or reject the page.
- Unreviewed items are never searchable. A document with unreviewed pages exposes only its reviewed pages, clearly marked partial.

---

## 6. Canonical Document Representation

The authoritative parsed artifact is a typed document tree. Markdown is a derived representation.

```text
Document
  Page
    Heading
    Paragraph
    List
    Table
    CodeBlock
    Figure
```

### Node metadata

```yaml
node:
  node_id: "<stable-node-id>"
  type: "paragraph"
  document_id: "<document-id>"
  document_version: "<version>"
  page_index: 0
  bounding_box: null
  heading_path: []
  parser: "native-or-olmocr"
  parser_revision: "<revision>"
  quality_status: "accepted"
```

Geometry is retained when available. Missing geometry must not be fabricated.

### Tables

Preserve:

- Cell contents.
- Row and column relationships.
- Header associations.
- Merged-cell information when available.
- Source page and bounding box when available.
- Parent table ID.
- A reference to the original page image.
- Detection provenance: `native-detected` vs `olmocr-routed`.

Use Markdown pipe tables for simple structures and HTML or structured JSON for complex tables.

Retrieval serialization may repeat headers and express rows as text, but must retain a link to the canonical table.

### Derived artifacts

Store:

- Canonical structured document.
- Markdown serialization.
- Selected page renders or review assets.
- Parsing diagnostics (including per-page routing decision and signal values).
- Parser and model provenance.

---

## 7. Structural Chunking and Context Preservation

### Chunking method

Traverse the canonical document tree and split along:

- Heading boundaries.
- Paragraph groups.
- Lists.
- Table boundaries.
- Code blocks.

Use tokenizer-aware sizing rather than character counts.

### Initial tuning targets

```yaml
chunking:
  prose_target_tokens: 420
  prose_overlap_tokens: 48
  preserve_heading_path: true
  preserve_parent_relationships: true
  split_tables_by_row_groups: true
  repeat_table_headers: true
```

These are starting targets, calibrated against the M1 corpus. The 420-token target is derived from the reranker budget: 512 (MiniLM max input) − ~60 (query + separators) − margin ≈ 440; 420 keeps passage + query inside the window without truncation.

### Rules

- Avoid crossing unrelated sections.
- Apply overlap selectively to prose.
- Do not duplicate entire tables through sliding overlap.
- Split oversized tables into row groups with repeated headers.
- Split oversized code blocks at meaningful boundaries where possible.
- Retain parent IDs and adjacent-chunk relationships.
- Preserve source-page ranges and provenance.
- Include heading context in embedding text.
- **Hard rule:** no chunk's tokenized length (with the reranker's tokenizer) may exceed `512 − max_query_tokens`; oversized chunks are split, never truncated.

---

## 8. Embedding Generation

### Model

```text
google/embeddinggemma-2
```

Apache 2.0, released 2026-10-06. 768-dimensional output, 8192-token context, 100+ languages. The encoders load independently — text (270M), vision (~170M), audio (~300M) — selected via `config_kwargs`. DRUID uses the **text+image configuration** (440M effective, `{"audio_config": None}`); the audio encoder is disabled. The approved fallback is the **text-only configuration** (270M, `{"vision_config": None, "audio_config": None}`).

Deployed on **CPU** by default (see §2 GPU arbitration) via sentence-transformers. The quantized text+image footprint is well under 1 GB RAM — the H200 stays uncontended for olmOCR.

### Task-aware input formatting (contractual)

EmbeddingGemma 2 requires asymmetric task prefixes. The wrong prefix is a quality bug, not a cosmetic choice. Defaults (validated against the M1 corpus):

```text
Document (prose/table/list chunks):
  title: {heading_path} | text: {chunk text}

Query:
  task: question answering | query: {user query}
```

CodeBlock chunks use the code-retrieval pair:

```text
Document: title: {filename or heading_path} | text: {code}
Query:    task: code retrieval | query: {user query}
```

The `SearchQuery` pair (`task: search result | query: {query}`) is the approved alternative for keyword-style search. Prompt selection is validated on M1, not assumed.

### Page-image embeddings

Text and images share one 768-dim vector space, so page renders are embedded with the vision encoder alongside text chunks — no separate index, no modality bridge:

- One record per page render: `page_start == page_end`, `metadata.modality = "page_image"`, `dense_vector` holds the image embedding, `text` holds a page label (not OCR output).
- Created for every page where a render exists at ingest. Visual-path pages always qualify; native-path pages qualify when the router flags significant visual content (figures, diagrams, image-dominated pages), rendering on demand.
- Text chunks carry `metadata.modality = "text"`. A single ANN search over `dense_vector` covers both modalities; text queries retrieve image records with no image input at query time.
- Modality mix in final results is tuned on the M1 corpus.

### Matryoshka dimensionality

Default is 768 dims. Truncation to 512, 256, or 128 (with re-normalization, up to ~6x storage reduction) is permitted after M1 validation. Any approved width must be applied identically at index and query time.

### Vector contract

```yaml
embedding_contract:
  model: "google/embeddinggemma-2"
  modality_config: "text+image"   # fallback: "text-only"
  dimension: 768                  # or validated Matryoshka width
  normalize: true
  task_prompts: "question-answering default; code-retrieval for code blocks"
  tokenizer_revision: "<pinned-revision>"
  model_revision: "<immutable-revision>"
  preprocessing_version: "<version>"
```

### Migration rule

Do not mix embeddings from different models, modality configs, Matryoshka widths, or preprocessing contracts.

A change to any of these requires:

1. A new versioned collection.
2. Complete re-embedding.
3. Retrieval validation against the M1 corpus.
4. Controlled alias cutover.
5. A rollback window.

---

## 9. Milvus Storage and Hybrid Retrieval

### Collection naming

Use a versioned physical collection and a stable application alias:

```yaml
milvus:
  collection: "project_knowledge_base_v1"
  alias: "project_knowledge_base"
  partition_key_field: "group_id"
```

### Schema

| Field | Type | Purpose |
|---|---|---|
| `chunk_id` | VarChar, primary key | Tenant-scoped unique chunk ID |
| `group_id` | VarChar, partition key | Tenant ownership |
| `document_id` | VarChar | Document lookup and deletion |
| `document_version` | VarChar | Version-scoped operations |
| `parent_id` | VarChar | Context expansion |
| `page_start` / `page_end` | Integer | Source attribution |
| `chunk_order` | Integer | Neighbor ordering |
| `text` | Analyzer-enabled VarChar | Retrieval text |
| `identifiers` | Array of VarChar | Exact-match identifiers (invoice nos., part numbers, clause refs) |
| `dense_vector` | FloatVector, 768 | Semantic retrieval — text chunks and page-image records share the unified space |
| `sparse_vector` | SparseFloatVector | Generated BM25 representation |
| `metadata` | JSON | Flexible provenance and structural attributes; includes `modality`: `"text"` \| `"page_image"` |

The `identifiers` field is populated during canonical-tree construction from patterns and table/heading structure (configurable per tenant). It backs the literal-match path in §10 — BM25 alone does not guarantee literal matching.

### Dense index

```yaml
dense_index:
  index_type: "HNSW"
  metric_type: "COSINE"
  params:
    M: 16
    efConstruction: 200
```

Tune search parameters using measured recall and latency on the M1 corpus.

### BM25 configuration

Built-in BM25 requires an analyzer-enabled text field and an explicit BM25 function. Its sparse index uses the `BM25` metric, not `IP`.

```yaml
text_field:
  enable_analyzer: true
  max_length: 65535

bm25_function:
  name: "text_to_bm25"
  type: "BM25"
  input_fields: ["text"]
  output_fields: ["sparse_vector"]

sparse_index:
  index_type: "SPARSE_INVERTED_INDEX"
  metric_type: "BM25"
```

This YAML expresses implementation intent; the agent must translate it into the pinned Milvus SDK.

### Partition behavior

Partition keys route tenants into hash partitions; they do not guarantee one physical partition per tenant. `partitionkey.isolation` is an optional HNSW optimization, not the authorization boundary for all access paths.

```yaml
properties:
  partitionkey.isolation: true
```

Use it only after compatibility testing with the pinned Milvus version. Authorization always lives in the application repository layer (§4), never in partition behavior.

### Capacity planning (single host)

Host RAM and disk must be sized, not assumed. Rough planning figures:

- Dense vector: 768 dims × 4 bytes ≈ 3 KB per chunk, plus HNSW graph overhead (~M×2 links).
- 1M chunks ≈ 3 GB vectors + index overhead; 10M chunks ≈ 30 GB + overhead.
- Page-image records add ≈ 3 KB per rendered page (same 768-dim math); size the image-record count from expected visual-path page volume.
- Milvus, PostgreSQL, MinIO, and the vLLM host-side footprint all share host RAM with the OS page cache.

Record the sizing calculation for the target corpus in the deployment runbook. The H200's 141 GB is *GPU* memory — Milvus does not live there.

---

## 10. Retrieval, Fusion, and Reranking

### Search sequence

1. Validate caller context.
2. Construct tenant and document-ACL predicates.
3. Exclude inactive, deleting, and unreviewed versions at the catalog layer (hard filter — never rely on index state alone).
4. Generate the query embedding with the task-appropriate prompt (CPU service).
5. Execute authorized dense retrieval across text and page-image records (single ANN search over the unified space).
6. Execute authorized BM25 retrieval.
7. Execute authorized literal identifier match (when the query contains identifier-like tokens).
8. Deduplicate and fuse candidates (RRF over dense + BM25; identifier hits merged with a fixed boost).
9. Rerank text records locally (CPU). Page-image records carry no rerankable text; they enter fusion on dense rank only.
10. Optionally expand authorized parent or neighboring content.
11. Reconcile candidate document versions with catalog state.
12. Return provenance-tagged passages and source references.

### Initial retrieval targets

```yaml
retrieval:
  dense_candidates: 50
  lexical_candidates: 50
  identifier_candidates: 20
  fusion: "reciprocal_rank_fusion"
  rerank_candidates: 50
  final_results: 8
```

These values are proposed starting points, tuned against the M1 corpus.

### Default reranker

```text
cross-encoder/ms-marco-MiniLM-L6-v2
```

### Optional reranker

```text
ContextualAI/ctxl-rerank-v2-instruct-multilingual-1b
```

Enable only after license approval. The selected checkpoint uses CC BY-NC-SA 4.0 and must not be assumed approved for enterprise/commercial deployment.

### Result contract

```yaml
result:
  document_id: "<document-id>"
  document_version: "<version>"
  chunk_id: "<chunk-id>"
  filename: "<display-name>"
  page_start: 1
  page_end: 2
  heading_path: ["Section", "Subsection"]
  text: "<retrieved-content>"
  rank: 1
  reranker_score: null
  source_reference: "<authorized-reference>"
  provenance: "native-detected | olmocr-routed"
  modality: "text | page_image"
  page_image_ref: "<object-storage-key | null>"
  trust: "untrusted-document-content"
```

Do not present ranking scores as calibrated probabilities.

### Untrusted content handling

Retrieved text is untrusted content. It must not alter authentication, tenant scope, tool permissions, or system instructions. Concretely:

- Every MCP/API response wraps passages with explicit provenance delimiters and a `trust: untrusted-document-content` marker so calling agents can distinguish tool output from instructions.
- The MCP server's tool descriptions instruct callers to treat `text` fields as data, never as instructions.
- Citation assembly never interpolates raw document text into system-adjacent fields.

---

## 11. MCP and API Interfaces

### Remote MCP transport

Use Streamable HTTP for the authenticated network service. Keep stdio as a separate local adapter with an explicit trusted credential context. Streamable HTTP and stdio are distinct transport modes; gateway headers do not automatically apply to stdio.

### Tools

| Tool | Behavior |
|---|---|
| `search_knowledge_base` | Authorized hybrid retrieval; returns provenance-tagged passages |
| `ingest_document` | Register an authorized upload and enqueue ingestion (quota-checked) |
| `get_ingestion_status` | Return authorized job status |
| `list_documents` | List authorized documents (supports `review_required` filter) |
| `get_document` | Return authorized metadata or artifact references |
| `get_review_item` | Return quarantine item: page render, parse attempts, routing diagnostics |
| `resolve_review_item` | Accept / re-route / reject a quarantined page (reviewer role required) |
| `delete_document` | Authorize and enqueue complete deletion |

Tool arguments must not expose unrestricted tenant IDs, arbitrary database filters, arbitrary filesystem paths, or unrestricted remote-fetch URLs.

Mutating tools should support confirmation in the calling client.

### Transport security

- Validate token signature, issuer, audience, and expiry.
- Enforce tool-specific roles or scopes (`resolve_review_item` requires a reviewer role).
- Validate HTTP `Origin` according to the MCP transport specification.
- Prevent direct unauthenticated access to internal services.
- Keep session identifiers separate from authentication identity.

---

## 12. Jobs, Versioning, and Deletion

### Ingestion states

```text
UPLOADED
  → QUEUED
  → PARSING
  → CHUNKING
  → EMBEDDING
  → INDEXING
  → READY

Exceptional states:
  RETRY_PENDING
  FAILED
  REVIEW_REQUIRED
  DELETING
  DELETED
```

### Version activation

Do not expose partially indexed versions.

Index a new version, verify expected artifacts and chunk counts **within the plausible range recorded for the active parser revision** (exact counts vary legitimately between revisions), then activate it in the catalog. Retrieval must exclude inactive, deleting, or unreviewed versions before returning results — enforced as a catalog-layer hard filter in the repository, independent of Milvus compaction state.

### Idempotency

Use stable job and chunk identifiers so retries do not create duplicates.

Content deduplication is tenant-scoped unless cross-tenant deduplication has undergone a separate security review.

### Deletion

Deletion must cover:

- Milvus chunks (note: physical removal follows compaction; the catalog hard filter in §10 covers the visibility window).
- Original files.
- Derived document artifacts.
- Retained page renders.
- Caches (keyed by tenant + version so stale entries cannot leak across versions).
- Catalog visibility.
- Applicable backup-retention handling.

Record completion and failures for reconciliation. A deletion job is not `DELETED` until every store confirms.

---

## 13. Validation and Acceptance Criteria

### M1: eval corpus

- 50–100 documents covering the parsing matrix below.
- 3–10 labeled Q/A pairs per document, including table questions and exact-identifier lookups.
- Versioned with the code; retrieval regressions block merges.

### Parsing quality

Test:

- Clean digital PDFs.
- Scanned documents.
- Mixed PDFs.
- Broken text layers.
- Multi-column layouts.
- Tables with merged cells (native-detected vs olmOCR-routed, measured separately).
- Long tables.
- Code and mathematical content.
- Blank and rotated pages.

Compare native-only, visual-only, and routed parsing on the same corpus. The router ships on measured page-level routing accuracy (target ≥95% vs human labels).

### Retrieval quality

Measure:

- Recall at candidate depth.
- Final ranking quality.
- Citation correctness.
- Table-question performance (split by parse path).
- Exact-identifier behavior (via the `identifiers` field path).
- Embedding configuration: task-prompt selection, Matryoshka width, and page-image recall (measured separately from text recall).
- Latency and throughput (p50/p99 for query; pages/hour for ingestion on the single H200).

### Security tests

Verify:

- Cross-tenant search, list, fetch, and deletion attempts fail.
- Dense, sparse, and identifier retrieval enforce identical authorization.
- Spoofed identity headers are rejected.
- Caller-supplied tenant IDs cannot affect writes.
- Object-storage references remain scoped.
- Cache entries cannot cross authorization boundaries.
- Deleted, deleting, inactive, and unreviewed versions do not appear in results.
- Startup and inference succeed without upstream network access.
- MCP responses carry provenance markers; a red-team prompt-injection pass over retrieved content does not alter agent authorization behavior.

### Operational acceptance

- Missing artifacts fail startup clearly.
- Retry behavior is idempotent.
- Worker failures do not expose partial versions.
- License approvals are recorded.
- Model and dependency revisions are reproducible.
- Logs carry request, job, and document IDs without unnecessary document content.
- **Backup/restore:** a full restore of Milvus + PostgreSQL + MinIO from backup returns the system to a queryable state; tested on a schedule, not just documented.
- **GPU arbitration:** olmOCR throughput is measured with the embedding and query services under load; no uncontended GPU sharing is permitted without re-validation.

---

## 14. Consolidated Implementation Specification

```yaml
system:
  name: "enterprise-rag-mcp"
  language: "Python 3.11+"
  mcp_framework: "mcp Python SDK / FastMCP"
  remote_transport: "streamable-http"
  local_transport: "stdio-adapter"
  dependency_versions: "pinned-lockfile"
  deployment_target: "single-nvidia-h200"

artifact_policy:
  olmocr2_approved: true
  production_visual_checkpoint: "allenai/olmOCR-2-7B-1025-FP8"
  bf16_checkpoint_role: "fine-tuning-only"
  chinese_model_ancestry_permitted: true
  staging_sources: "explicit-allowlist"
  prohibited_sources:
    - "modelscope.cn"
    - "Chinese mirror repositories"
    - "unapproved endpoints"
  runtime_downloads: false
  immutable_revisions_required: true
  checksums_required: true
  runtime_network: "approved-internal-destinations-only"

compute:
  gpu_consumer: "vllm-olmocr-fp8-only"
  gpu_memory_cap_initial: 0.70
  embedding_service: "cpu-default"
  reranker_service: "cpu"
  host_sizing: "recorded-in-deployment-runbook"

parsing:
  docling_enabled: false
  native_backend: "pdftext"
  optional_native_backend: "pymupdf-license-approved"
  native_workers: "process-based (pdfium-not-thread-safe)"
  routing_unit: "page"
  table_region_detection: "native-geometry-heuristics-with-olmocr-fallback"
  visual_checkpoint: "allenai/olmOCR-2-7B-1025-FP8"
  serving_backend: "vllm"
  render_spec: "longest-dimension-1288px-toolkit-prompt-builders"
  canonical_output: "typed-document-tree"
  derived_output: "markdown"
  low_quality_action: "retry-or-review-queue"
  router_milestone: "M1-eval-corpus-first"

chunking:
  strategy: "structure-first-tokenizer-aware"
  prose_target_tokens: 420
  prose_overlap_tokens: 48
  preserve_heading_path: true
  preserve_table_structure: true
  preserve_parent_relationships: true
  reranker_budget_enforced: true
  reranker_max_input_tokens: 512

embeddings:
  primary: "google/embeddinggemma-2"
  modality_config: "text+image"          # 440M effective; audio encoder disabled
  fallback_config: "text-only"          # 270M; vision+audio disabled
  model_path: "<approved-local-path>"
  dimension: 768                        # Matryoshka 512/256/128 permitted after M1 validation
  normalize: true
  task_prompts: "question-answering default; code-retrieval for code blocks"
  page_image_records: true              # modality="page_image", unified vector space
  model_change_requires_reindex: true

milvus:
  collection: "project_knowledge_base_v1"
  alias: "project_knowledge_base"
  partition_key_field: "group_id"
  authorization_boundary: "tenant-scoped-application-repository"
  optional_properties:
    partitionkey.isolation: true   # test-first; never the auth boundary
  dense_index:
    index_type: "HNSW"
    metric_type: "COSINE"
    params:
      M: 16
      efConstruction: 200
  lexical_index:
    index_type: "SPARSE_INVERTED_INDEX"
    metric_type: "BM25"
    analyzer_enabled: true
    bm25_function_required: true
  identifier_field: "identifiers (Array[VarChar])"

retrieval:
  dense_candidates: 50
  lexical_candidates: 50
  identifier_candidates: 20
  fusion: "reciprocal_rank_fusion"
  rerank_candidates: 50
  final_results: 8
  authorize_before_retrieval: true
  hard_exclude_inactive_versions: true
  verify_active_versions_before_return: true

reranking:
  primary: "cross-encoder/ms-marco-MiniLM-L6-v2"
  optional_license_approval_required:
    - "ContextualAI/ctxl-rerank-v2-instruct-multilingual-1b"
  enforce_model_input_budget: true

operations:
  document_catalog: "postgresql"
  object_storage: "minio-s3-compatible"
  durable_queue: "redis-backed"
  idempotent_jobs_required: true
  versioned_index_activation: "range-based-count-checks"
  complete_deletion_workflow: true
  review_queue: "quarantine-api-with-reviewer-role"
  backup_restore_tested: true

quotas:
  ingestion_pages_per_day_per_tenant: 1000
  query_qps_per_tenant: 10
  max_upload_mb: 200
  max_pages_per_document: 2000

hard_rules:
  trusted_tenant_context: >
    Derive tenant identity only from validated authentication or
    authenticated gateway context. Never accept tenant identity from
    document content, tool arguments, or untrusted headers.

  scoped_data_access: >
    Enforce tenant ownership and applicable document ACLs across Milvus,
    catalog records, object storage, background jobs, and caches.

  safe_writes: >
    Set tenant ownership server-side. Use tenant-scoped IDs and verify
    ownership for updates and deletions.

  approved_artifacts_only: >
    Stage all artifacts from allowlisted sources. Prohibit ModelScope,
    Chinese mirror repositories, and unapproved endpoints. Load only
    pre-staged verified artifacts in production.

  structured_document_fidelity: >
    Preserve a canonical typed document representation, table structure,
    page provenance, and parent relationships. Markdown is a derived
    serialization, not the only retained representation.

  no_partial_visibility: >
    Incomplete, failed, inactive, deleting, or unreviewed document versions
    must not be returned as searchable content. Enforcement is a catalog-layer
    hard filter, independent of index compaction state.

  untrusted_document_content: >
    Retrieved content cannot override identity, authorization, system
    instructions, or tool permissions. MCP/API responses wrap passages with
    explicit provenance markers so calling agents can distinguish data from
    instructions.

  fail_closed: >
    Missing authentication, tenant context, required artifacts, or
    authorization must produce an explicit failure rather than fallback
    to unrestricted access or external downloads.

  gpu_discipline: >
    Exactly one GPU consumer (vLLM serving olmOCR FP8) with an explicit
    memory cap. Embedding and reranking default to CPU. Any additional GPU
    consumer requires re-validation of the arbitration policy.
```

---

## 15. Open Decisions (require owner sign-off)

1. **Infra defaults** (§1): PostgreSQL / Redis / MinIO are defaults. Confirm or replace.
2. **Quota values** (§4): initial ingestion/query quotas are estimates against one H200 — validate with a throughput burn-in.
3. **Table detection default** (§5): native-geometry heuristics with olmOCR fallback. If the M1 corpus shows native detection underperforming, flip the default to route all table-signal pages to olmOCR.
4. **Rerank width** (§10): 50 candidates is a starting point; widen if table/exact-identifier recall lags on the M1 corpus.
5. **HA posture**: v1 accepts a single point of failure with tested backups. If uptime requirements tighten, this section gets rewritten — not patched.
6. **Embedding task prompt + Matryoshka width** (§8): QuestionAnswering prompts at 768 dims are the default. If M1 shows the SearchQuery pair or a truncated width (512/256/128) winning on recall-per-GB, the default changes — validated, not assumed.
