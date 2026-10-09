# new_DRUID — Architecture & Services

Single H200 host. Everything except vLLM runs on CPU/host RAM. All compose
projects attach to the shared external `druid-net` network for cross-project
DNS (ingestion workers → `vllm-olmocr`).

```
clients (Web UI / AI agent / CLI / browser)
  → gateway (JWT auth, quotas, HMAC-signed identity) ── port 8080 host / 8000 container
      → document_api (upload / status / list / delete / review / admin) ── 8001
      → mcp_server (FastMCP Streamable HTTP /mcp, 8 tools) ── 8002
  → web_ui (FastAPI + Jinja2 + vendored htmx; thin UI over gateway only) ── 8005
  → catalog (PostgreSQL 16 + Alembic; tenant-scoped repository)
  → job queue (Redis + RQ; queues: ingest, delete)
  → ingestion workers (scalable: --scale ingestion=N)
        pdftext native path → page router → olmOCR 2 FP8 (via vLLM) →
        canonical tree → structural chunking → EmbeddingGemma 2 client (CPU)
  → embedding service (EmbeddingGemma 2, CPU, port 8003)
  → retrieval service (port 8004; hybrid search + MiniLM rerank, CPU)
  → Milvus standalone v2.5.6 (dense 768-dim + BM25 hybrid, group_id partition key)
  → RustFS (tenant-scoped object storage: originals / renders / trees)
  → vllm-olmocr (GPU plane; ONLY GPU consumer) ── 8000 (separate compose project)
```

## Services

### gateway (port 8080 host)
Authenticated reverse proxy. Per request: strip spoofable headers →
validate Bearer JWT (signature, issuer, audience, expiry; HS256 secret or
RS256 via JWKS) → build trusted `RequestContext` from claims only → enforce
quotas → forward with HMAC-signed `X-Druid-*` identity headers
(`X-Druid-User/Group/Roles/Request-Id/Internal-Sig`, HMAC-SHA256,
payload `druid-v1|user|group|roles_sorted_csv|request_id`, signature
`v1:<hex>`). Routes: `GET /healthz` (public), `POST /v1/documents/upload`
(ingestion quota), `POST /mcp` (quota class from tool name), catch-all
`/v1/{path}` → document_api. Quota exhaustion → `429` with `Retry-After`.

### document_api (port 8001)
Document + review-queue API, always behind the gateway. Routes:
upload (multipart, magic-byte MIME sniff, size cap streamed, PDF trailer
page count, sha256, tenant-scoped RustFS keys, commit-before-enqueue),
list/get/delete/status, review items + resolve, admin routes.
Verifies the gateway's `X-Druid-*` HMAC signature on every request and
fails closed without it.

### catalog (PostgreSQL 16 + Alembic)
Ownership, versions, jobs, review quarantine. `CatalogRepository` scopes
every query to the context's tenant (the `group_id` claim resolves to a
tenant row — a forged claim string alone is useless). Key behavior:
- `searchable_versions_query()` — the hard filter: `is_active` + status
  `READY` + document not deleted. Inactive, deleting, failed, or
  unreviewed versions are never searchable, independent of Milvus
  compaction.
- `activate_version()` — transactional check-and-swap gated on: no pending
  quarantine pages → version in `INDEXING` → chunk count present → chunk
  count within `[min,max]_chunks_per_page × page_count` from
  `parser_revision_stats` → document not deleted. Then deactivates the
  previous version and sets the new `READY`/`is_active`.
- Quota/quarantine RBAC: resolving quarantine items requires the
  `reviewer` role; deletion is catalog soft-delete → index sink → object
  prefix delete → `DELETED`.
- Migrations: `alembic upgrade head` runs as a one-shot compose job; the
  migration module is verified to import cleanly but has not run against
  live PostgreSQL (needs the H200 host).

### mcp_server (port 8002)
FastMCP server, Streamable HTTP mounted at `/mcp`, authenticated per
request: Bearer JWT required (→ 401), Origin allowlist for browser clients
(→ 403 on mismatch), spoofable identity headers stripped, `mcp-session-id`
never used as identity. `TOOL_ROLES` maps each tool to allowed roles;
`get_review_item` / `resolve_review_item` require reviewer/admin, the rest
any authenticated role. Tool docstrings carry the data-not-instructions
guidance; `delete_document` / `resolve_review_item` require
`confirm=true`. A separate **stdio adapter** exists with an explicit
`--user-id/--group-id/--roles` trusted context (gateway headers do not
apply). Tool reference: [MCP tools](druid-mcp-tools.html).

### ingestion (workers; scale with `--scale ingestion=N`)
RQ workers running `run_ingestion_job` / `run_deletion_job`
(entry points dotted into `services.ingestion.pipeline`):
1. **Native path** — pdftext extracts text per page (processes only, never
   threads; `PDFTEXT_WORKER_PAGE_THRESHOLD`).
2. **Page router** — `route_page()` scores each page → `NATIVE` / `VISUAL`
   / `SKIP_BLANK` from pure signal functions (text density, replacement-char
   ratio, private-use ratio, extraction-vs-geometric order disagreement,
   column clustering, table signals). Thresholds are provisional pending
   M1 eval-corpus calibration (≥95% routing-accuracy gate).
3. **Visual path** — page renders (longest dim 1288 px) go to
   olmOCR-2-7B-FP8 served by vLLM; prompts built only via the olmOCR
   toolkit's `build_no_anchoring_v4_yaml_prompt` (fail-closed ImportError).
   Low-confidence table pages and olmOCR failures → `quarantine_page()`
   → `REVIEW_REQUIRED` (parked, never retried blindly).
4. **Canonical tree** — native and visual parses normalize to a typed
   document tree (`stable_node_id()`, content-derived, retry-idempotent);
   tables from native geometry carry `native-detected` provenance, from
   olmOCR carry `olmocr-routed`.
5. **Structural chunking** — prose 420/48 with paragraph-boundary overlap
   (prose only); tables split by row groups with repeated headers; code
   split at blank lines; figures as render-reference chunks. Hard rule:
   `token_len ≤ 512 − max_query_tokens` asserted per chunk (violations
   raise, never truncate); production tokenizer is the pre-staged MiniLM
   (`MINILM_TOKENIZER_PATH`, fail-closed).
6. **Embed** — EmbeddingGemma 2 CPU service with task-aware prefixes
   (`title: {heading} | text: {chunk}` documents; `task: question
   answering | query: {q}` queries; code pair for code blocks).
7. **Index + activate** — Milvus upserts, then `activate_version()`.

### embedding (port 8003)
Standalone CPU FastAPI service for EmbeddingGemma 2 (text+image,
Matryoshka 768 — validated widths 768/512/256/128). Reuses the ingestion
sibling's prefix contract module (byte-equality asserted by test).
`POST /embed` (documents + queries), `POST /embed_image` (b64 PNG →
unified-space vector; `object_key` variant returns 501 until RustFS wiring
lands at deploy time), `GET /health`. Lifespan warmup forces backend load:
missing artifacts fail startup loudly. The ST backend is coded but has
never run against real EmbeddingGemma 2 weights (needs the H200 host).

### retrieval (port 8004)
CPU-only hybrid search; 12-step `service.search()`:
1. validate context (fail closed); 2. one `AuthPredicate` for all branches;
3. catalog hard filter; 4. task-appropriate query prefix (QA default,
   code-retrieval heuristic, `task_hint` override); 5. authorized dense
   (unified 768-dim text+page_image space) + BM25 + literal identifier
   match; 6. dedupe + RRF (k=60) with fixed identifier boost;
7. CPU MiniLM rerank of TEXT records only (page_image bypasses, re-enters
   by fusion score, capped at `max_page_image_results=2`);
8. optional neighbor expansion (default off); 9. catalog reconciliation;
10. §10 result contract with `trust: "untrusted-document-content"`.
Tunables (env-overridable): dense 50 / lexical 50 / identifier 20 /
rerank 50 / final 8. `POST /search` verifies the gateway's HMAC
`X-Druid-*` headers.

### milvus_setup (one-shot job)
pymilvus job creating `project_knowledge_base_v1` + alias
`project_knowledge_base`, idempotent, fail-closed schema-compatibility
check on existing collections (never auto-migrates). Schema: 13 fields —
`chunk_id` PK, `group_id` (partition key), `document_id`,
`document_version`, `parent_id`, `page_start`/`page_end`, `chunk_order`,
analyzer-enabled `text` (65535), `identifiers` array, `dense_vector`
768 float, `sparse_vector` (BM25 function `text_to_bm25`), `metadata`
JSON incl. `modality`. Indexes: HNSW/COSINE (M=16, efConstruction=200) +
`SPARSE_INVERTED_INDEX`/BM25. `partitionkey.isolation` off by default.

### artifacts (manifest tooling)
Manifest schema validation, source allowlist (modelscope.cn and mirrors
prohibited), revision-pin warnings, sha256 verification (`validate` /
`verify` CLI commands). Staged manifests for the three approved
checkpoints live in `services/artifacts/manifests/`; completed versions
under `manifests/completed/`.

### web_ui (port 8005)
FastAPI + Jinja2 + vendored htmx 2.0.4 (no CDN, offline-first). Thin human
layer: talks only to the gateway with the user's own Bearer `<redacted>` (no
direct Postgres/Milvus/RustFS). Paste-provisioned-JWT login; dark mode
default. See [Web UI](druid-web-ui.html).

## Ingest data flow

```
upload (gateway quota-check → document_api: validate, store in RustFS,
        catalog row, durable job, RQ enqueue)
  → ingestion worker (RQ `ingest` queue, rebuilds trusted context from job row)
  → native extraction / router / olmOCR / tree / chunker / embed / Milvus
  → activate_version() (quarantine gate, chunk-count range check)
  → version READY / searchable (or REVIEW_REQUIRED if pages parked)
```

## Query data flow

```
client → gateway (auth + QPS quota)
  → mcp_server: search_knowledge_base (Streamable HTTP /mcp, bearer JWT)
    → retrieval service (dense + BM25 + identifiers, RRF, MiniLM rerank)
    → catalog hard filter + reconciliation
    → provenance-tagged passages (filename + page range)
  → document_api: GET /v1/documents/... (gateway catch-all /v1/{path})
  → web_ui: /search page renders provenance-tagged passages via POST /mcp
```

Tenant identity is derived from the validated token at every hop; it is
never accepted from arguments, headers, or document content.

## Status notes (implementation vs planned)

- **Implemented and tested:** all services above, 273 passed / 4 skipped
  (live-Milvus gates skipped; need the H200 host).
- **Coded, not run against real infrastructure:** Alembic against real
  PostgreSQL; Redis Lua quota scripts; Milvus setup/idempotency/alias
  cutover; real EmbeddingGemma 2 backend warmup; vLLM olmOCR smoke.
- **Not started:** `eval/` corpus scaffolding (M1 milestone); model
  weights not staged; pip-compile not run (dependency pins are initial
  lockfiles — regenerate before release).
