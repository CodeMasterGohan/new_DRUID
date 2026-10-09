# new_DRUID build log

Running log of what was built, by whom, and what was stubbed. Each builder
appends their section. Spec source of truth: `../new_DRUID_architecture_v2.md` (v2.1).

---

## Scaffold / deploy / artifact tooling — 2026-10-08 (subagent)

### Built

- **Repo tree**: `README.md` (overview, quickstart, layout, offline policy),
  root `pyproject.toml` (ruff/mypy/pytest config, Python 3.11+),
  `.env.example` (all service settings: PG, Redis, MinIO, Milvus, JWT, quotas,
  model dirs, vLLM), `.gitignore` (excludes `.env`, `/models`, corpora).
- **Per-service `requirements.txt`** (9 services) with `==` pins, each headed
  "regenerate with pip-compile before release". Service set: gateway,
  document_api, mcp_server, catalog, ingestion, embedding, retrieval,
  milvus_setup, artifacts.
- **Dockerfiles** (9, one per service dir): `python:3.11-slim`,
  `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1` baked in. Notes embedded:
  ingestion warns PDFium is process-only; embedding installs CPU-only torch
  and must never request GPU; models mount read-only.
- **`deploy/docker-compose.yml`**: postgres:16.6-alpine, redis:7.4.2-alpine,
  MinIO, etcd + Milvus v2.5.6 standalone (Milvus shares our MinIO, separate
  bucket), one-shot `catalog` (alembic) and `milvus_setup` jobs, all CPU
  services with health-gated dependencies. Ingestion scales via
  `--scale ingestion=N`.
- **`deploy/docker-compose.gpu.yml`**: vLLM only, nvidia device reservation,
  `--gpu-memory-utilization 0.7` (from `${VLLM_GPU_MEMORY_UTILIZATION}`).
- **`deploy/vllm-olmocr.md`**: checkpoint rationale (FP8 production, BF16
  fine-tune-only), launch command, HBM3e memory math (~7 GB weights,
  ~98.7 GB vLLM reservation, ~42 GB deliberate headroom), request contract
  (toolkit prompt builders, 1288 px renders), smoke tests, tuning guidance.
- **`services/artifacts/`**: manifest tooling per §3 —
  `src/artifact_manifest.py` (schema validation, source allowlist,
  revision-pin warnings, sha256 verification, optional redirect-target
  check), `src/cli.py` (`validate` / `verify` commands), 3 example manifests
  (olmOCR-2-7B-1025-FP8, embeddinggemma-2 text+image, MiniLM-L6-v2) as
  **templates** (zeroed sha256, unfilled revision — must be completed at
  staging), `README.md`, 21 unit tests.
- **`docs/runbook.md`**: bring-up order, smoke tests, offline verification,
  quota burn-in, GPU arbitration check, and the §9 host-sizing math
  (worked example: 5M chunks + 200K page images → ~43 GB Milvus-side,
  ≥96 GB host RAM recommended; ~8 KB/chunk rule of thumb; GPU RAM excluded
  from the budget by design).
- **`docs/backup-restore.md`**: per-store tools, catalog+Milvus consistency
  window, monthly restore drills (pass/fail), RPO ≤24h / RTO ≤4h initial,
  deletion-vs-backup-retention note.

### Decisions made

- Infra defaults from spec v2.1 §15 (Postgres/Redis/MinIO) taken as-is.
- Milvus standalone reuses the tenant MinIO (separate bucket) instead of a
  second MinIO — one fewer stateful service.
- `rq` chosen for the Redis-backed queue (simplest durable option); swappable.
- `mcp==1.9.0` (MCP Python SDK incl. FastMCP) for the MCP server.
- MinIO image left as `latest` with a TODO to pin a dated RELEASE tag —
  could not verify a current tag offline.
- Dependency pins are initial, not verified — flagged in every file and the
  root README.

### Tests

- `services/artifacts`: **21/21 pass** (venv, pyyaml+pytest). Covers schema
  validation, allowlist/prohibited hosts (incl. modelscope.cn + subdomains,
  hf-mirror.com), https enforcement, mutable-revision warnings, checksum
  verify/mismatch/missing/duplicate.
- CLI smoke-tested: `validate` on the olmOCR template (warns on unfilled
  revision placeholder, exits 0); modelscope manifest rejected end-to-end.

### Not done / gaps

- Service application code (gateway, APIs, workers, MCP tools, catalog
  models, retrieval, milvus_setup job) — other agents' scope; Dockerfiles
  reference their expected module entrypoints (`main:app`, `-m server`,
  `-m create_collection`).
- Dependency pins unverified against PyPI (no `pip-compile` run here).
- MinIO image tag unpinned (see above).
- `eval/` corpus scaffolding not started (M1 milestone).

---

## MCP server — 2026-10-08 (subagent)

### Built

`services/mcp_server/` — complete FastMCP implementation of spec §11/§10/§4:

- **`src/mcp_server/`** (8 modules):
  - `context.py` — `RequestContext` (user_id, group_id, roles, server-side request_id) in a contextvar; `get_context()` fails closed when absent. Tenant identity never comes from args, metadata, headers, session IDs, or paths.
  - `auth.py` — `TokenVerifier` ABC + `JWTVerifier` (signature, issuer, audience, expiry via PyJWT; fails closed on any error); `TOOL_ROLES` map (`resolve_review_item` and `get_review_item` require reviewer/admin; everything else any authenticated role).
  - `models.py` — pydantic arg validation: opaque IDs (`^[A-Za-z0-9_-]{1,128}$`, rejects `../`, absolute paths, etc.); filenames are bare display names with allowlisted extensions; MIME allowlist (pdf/png/jpeg/tiff); sha256 format; 200 MB cap; bounded `top_k`/limits; `confirm` flags. §10 result contract: `Passage` renders with `<<<druid:passage>>>` delimiters carrying `trust="untrusted-document-content"`, modality (`text`|`page_image`), `page_image_ref`; provenance attribute escaping rejects quote/bracket injection.
  - `clients.py` — ABCs `RetrievalServiceClient`, `IngestionServiceClient`, `CatalogClient` (context-first signatures; implementations enforce tenant predicates server-side) + in-memory fakes for tests/dev.
  - `tools.py` — all 8 tools; every docstring instructs callers to treat `text` as untrusted data, never instructions. Mutating tools (`delete_document`, `resolve_review_item`) require `confirm=true`; `ingest_document` registers an upload ticket (bytes go via Document API — no remote-URL fetching anywhere).
  - `server.py` — `create_app()` factory: FastMCP Streamable HTTP mounted at `/mcp` behind `AuthMiddleware` (Bearer validation → 401; Origin allowlist → 403 on mismatch; spoofable identity headers stripped; server-side request_id; session IDs never used for identity). Refuses to build without real service clients unless `DRUID_MCP_DEV_FAKE_CLIENTS=1`.
  - `stdio_adapter.py` — separate stdio entrypoint with explicit `--user-id`/`--group-id`/`--roles` trusted context (fail-closed); gateway concepts do not apply.
- **`Dockerfile`** — replaced the scaffold's placeholder CMD (`python -m server …`, which referenced a nonexistent module) with `uvicorn --factory mcp_server.server:create_app` on port 8002, non-root user, offline env baked in. **`requirements.txt`** — re-pinned to the versions this code was built and tested against (`mcp==1.12.0`, `pydantic==2.11.7`, `PyJWT==2.10.1`, `httpx==0.28.1`, `uvicorn==0.34.3`, `pytest==8.3.5`, `pytest-asyncio==0.26.0`); scaffold pins were explicitly unverified.
- **`.env.example`** — appended MCP section (`JWT_SECRET`/`JWT_PUBLIC_KEY_PEM`, `MCP_ALLOWED_ORIGINS`); server now reads the shared `JWT_ISSUER`/`JWT_AUDIENCE` names.
- **`README.md`** — transports, tools, config, run instructions, layout, gaps.
- **`tests/`** — 74 contract tests.

### Decisions made

- Env names aligned to the shared `.env.example` (`JWT_*`, `MCP_ALLOWED_ORIGINS`) instead of `DRUID_`-prefixed names.
- `get_review_item` restricted to reviewer/admin (quarantine surface shows unreviewed content).
- Quota enforcement stays at the gateway per §4; MCP server does not duplicate it.
- Provenance attribute validation happens at render time (`ValueError` on illegal chars), not model construction.
- `.venv/` lives inside `services/mcp_server/` (gitignored) for running tests here.

### Tests

- **74/74 pass** (`PYTHONPATH=src .venv/bin/python -m pytest tests/ -q`): 8 tool schemas registered with exact §11 names; descriptions contain the data-not-instructions instruction; no schema exposes tenant/filter/path/URL params; arg validation (opaque IDs, filenames, MIME, sha256, bounds, enums, confirm gates); JWT accept/reject matrix; role enforcement; fail-closed missing context; provenance rendering + attribute-injection rejection; middleware 401/403 matrix incl. spoofed-header stripping and session-ID isolation; stdio trusted-context validation.
- Smoke-tested: `create_app()` builds; tool listing works; stdio `--help` works.

### Not done / gaps

- Real downstream clients (HTTP to retrieval/ingestion/catalog services) — sibling agents own those services; ABCs are the contract.
- `delete_document`/`resolve_review_item` return confirmation-required responses when `confirm=false`; the calling client must re-invoke.
- Dependency pins verified for this service only (installed + tested); run `pip-compile` before release per repo policy.


---

## Catalog + gateway — 2026-10-08 (subagent)

Built against spec v2.1, §§4, 5, 12. **35/35 tests pass**
(`python -m pytest services/catalog/tests services/gateway/tests -q` from
the build root; SQLite for catalog, mocked upstream for gateway).

### Built

**`services/common/request_context.py`** (new shared package)
- Frozen `RequestContext` dataclass: `user_id`, `group_id`, `roles`,
  `request_id`. Rejects empty identity at construction (fail closed). Shared
  so catalog, gateway, and later services use one §4 type instead of
  divergent copies.

**`services/catalog/`**
- `models.py` — `Tenant`, `Document` (opaque id, `active_version_id`
  pointer, soft-delete), `DocumentVersion` (full `IngestionState` lifecycle,
  denormalized `tenant_id`, `parser`/`parser_revision`, `page_count`,
  `chunk_count_actual`, `is_active`), `Job` (per-tenant unique
  `idempotency_key`, persisted `trusted_user_id`/`trusted_group_id`/
  `trusted_roles`), `ReviewQuarantineItem` (page render ref, both parse
  attempts, routing diagnostics, reviewer decision), `DocumentACL`,
  `ParserRevisionStats` (min/max chunks-per-page per parser revision).
  JSONB on PostgreSQL, plain JSON on SQLite.
- `repository.py` — `CatalogRepository`, the §4 tenant-scoped repository:
  every query filtered to the context's tenant (the `group_id` *claim* is
  resolved to the tenant UUID via an active tenant row — a forged claim
  string alone is useless); `create_document` ignores caller-supplied
  `tenant_id`; `get_document` raises `NotFoundError` cross-tenant;
  `searchable_versions_query()` is the hard filter (`is_active` +
  `status == READY` + document not deleted), independent of Milvus
  compaction; quarantine CRUD with reviewer-role-gated resolution
  (accepted | re-routed | rejected); `check_permission` implements the
  group-wide default with ACL enforcement when rows exist;
  `revalidate_job_context` for workers.
- `activation.py` — `activate_version()`: transactional check-and-swap.
  Check order: pending quarantine pages → `INDEXING` status → chunk-count
  present → range check (`[min,max]_chunks_per_page × page_count` from
  `parser_revision_stats`) → document not deleted → deactivate previous
  active version, set new `READY`/`is_active`, move
  `document.active_version_id`. Flushes; caller owns commit/rollback.
- `errors.py` — `CatalogError` hierarchy, all fail closed.
- `alembic/` — `alembic.ini`, `env.py` (requires `DATABASE_URL`, refuses to
  guess), `script.py.mako`, `versions/0001_initial_schema.py` (full schema
  incl. `ingestion_state` enum type and the deferred
  `documents.active_version_id` FK). Migration module imports cleanly; not
  executed (no live PostgreSQL here).
- `requirements.txt` — re-pinned to install-verified versions
  (`sqlalchemy==2.0.38`, `alembic==1.15.2`, `psycopg[binary]==3.2.9`,
  `pydantic==2.10.6`, `pytest==8.3.5`); kept the scaffold's `structlog`.
- `README.md`, `tests/test_catalog.py` (17 tests: tenant isolation,
  context-required, activation happy/out-of-range/wrong-status/no-baseline/
  quarantine-blocked paths, hard-filter matrix, job idempotency + context
  revalidation, quarantine RBAC + scoping, ACL default/enforcement).

**`services/gateway/`** (FastAPI, §4)
- `app.py` — `create_app(settings)` factory (+ module-level `app` for
  uvicorn). Per request: strip spoofable headers (middleware) → validate
  JWT → trusted context from token claims only → quota → forward. Routes:
  `GET /healthz` (public), `POST /v1/documents/upload` (413 over size cap,
  400 on bad `page_count`, ingestion quota), `POST /mcp` (quota class from
  tool name: `ingest_document` → ingestion quota, else query QPS —
  arguments influence quota class only, never identity), catch-all
  `/v1/{path}` → document API (query quota). Quota exhaustion → 429 with
  `Retry-After` (rounded up, min 1s).
- `auth.py` — JWT validation: signature, issuer, audience, expiry; HS256
  shared secret + RS256/ES256 via JWKS URL (cached); allowlisted
  algorithms; `group_id` claim required, never falls back to headers.
- `druid_internal.py` — HMAC-SHA256 signing/verification of forwarded
  identity (`X-Druid-User/Group/Roles/Request-Id/Internal-Sig`); header
  contract documented for sibling services; `SPOOFABLE_HEADERS` includes our
  own `X-Druid-*` so clients can't pre-seed them.
- `quotas.py` — `QuotaConfig` (§4 defaults), `QuotaStore` interface,
  `InMemoryQuotaStore` (sliding-window QPS, daily ingestion),
  `RedisQuotaStore` (atomic Lua; rolls back rejected ingestion consumption
  so accounting stays exact).
- `proxy.py` — upstream forwarding; drops hop-by-hop + spoofable headers;
  forwards bearer token AND signed identity headers; 502/504 mapping.
- `requirements.txt` — re-pinned to install-verified versions (added
  `cryptography==44.0.2` for RS256, `pytest==8.3.5`; kept `structlog`).
- `README.md`, `tests/test_gateway.py` (18 tests: token→context, spoofed
  headers stripped+ignored, MCP `group_id` arg ignored, 401 matrix
  incl. missing `group_id` claim, QPS/ingestion 429s with `Retry-After`,
  413/400 upload guards, internal sign/verify round-trip + tamper rejection).

### Decisions made
- **Env names aligned to the shared `.env.example`** (no `DRUID_` prefix:
  `JWT_SECRET`, `JWT_ISSUER`, `JWT_AUDIENCE`, `JWKS_URL`,
  `QUOTA_INGEST_PAGES_PER_DAY`, `QUOTA_QUERY_QPS`, `MAX_UPLOAD_MB`,
  `MAX_PAGES_PER_DOCUMENT`, `REDIS_URL`), matching the MCP sibling's
  decision. `DOCUMENT_API_URL`, `MCP_SERVER_URL`, `INTERNAL_SECRET` are new
  names the coordinator should add to the shared template.
- Idempotency conflicts use an explicit pre-check + DB unique-constraint
  backstop, not savepoints — a mid-transaction `rollback()` would nuke the
  caller's ambient work (found the hard way; see tests).
- Quarantine check runs before the `INDEXING` gate in activation so a
  parked version reports "unreviewed pages" instead of a generic mismatch.
- `services/__init__.py` added as a namespace marker for pytest imports.

### Not done / gaps
- `RedisQuotaStore` Lua scripts are not exercised against a live Redis (no
  server here); needs an integration test.
- Alembic migration targets PostgreSQL; not executed against live PG.
- RS256/JWKS auth path is coded but only HS256 is tested (no JWKS endpoint
  in test env).
- Gateway upstreams (`DOCUMENT_API_URL`, `MCP_SERVER_URL`) are sibling
  agents' scope; the `X-Druid-*` header contract in `druid_internal.py` is
  the integration point.
- `services/milvis_setup` typo noticed, left alone (not my scope).

## 2026-10-08 — services/retrieval (retrieval agent)

### What was built
- `services/retrieval/`: full §10 retrieval service, CPU-only, stdlib-only
  core (dataclasses + logging) so unit tests run without heavy installs.
  - `service.py` — `RetrievalService.search(context, query)` implementing the
    12-step sequence exactly: validate context (fail closed) → one
    `AuthPredicate` for all branches → catalog hard filter via
    `CatalogSearchPort` → task-appropriate EmbeddingGemma 2 query prompt
    (QA default / code-retrieval heuristic + `task_hint` override /
    search-result alternative) → authorized dense (unified 768-dim space,
    text + page_image) + BM25 + literal identifier match → dedupe + RRF
    (k=60) with fixed identifier boost → CPU rerank of TEXT records only
    (page_image bypasses, re-enters by fusion score) → optional authorized
    neighbor expansion → catalog reconciliation (race safety) → §10 result
    contract with `trust: "untrusted-document-content"`.
  - `config.py` — tunable targets (dense 50 / lexical 50 / identifier 20 /
    rerank 50 / final 8, `max_page_image_results=2`), all env-overridable.
  - `interfaces.py` — injectable `EmbeddingClient`, `VectorStore`,
    `CatalogSearchPort`, `Reranker` (MiniLM `cross-encoder/ms-marco-MiniLM-L6-v2`).
  - `predicates.py` — single predicate object per search, identical on dense /
    sparse / identifier branches; Milvus expr rendering with string escaping.
  - `prompts.py`, `identifiers.py`, `fusion.py`, `reranker.py` (budget
    truncation guard), `citations.py` (metadata-only source references,
    provenance delimiters + trust marker wrapping, citation assembly never
    touches raw document text), `context.py` (fail-closed validation),
    `catalog_port.py` (`SqlAlchemyCatalogPort` — uses
    `CatalogRepository.searchable_versions_query`, never reimplements).
  - `main.py` — FastAPI `POST /search` (port 8004); verifies the gateway's
    HMAC-signed `X-Druid-*` identity headers before building the trusted
    context; returns `results` (contract dicts) + `passages` (wrapped).
  - `tests/test_retrieval.py` (25 tests) + `tests/conftest.py`, `README.md`;
    `pytest==8.3.5` added to `requirements.txt`.

### Decisions made
- `repository.search(context, query)` (§4) is `RetrievalService.search` —
  the service IS the tenant-aware repository.
- Milvus string-encoding contract (documented in `catalog_port.py`):
  `document_id == str(Document.id)`, `document_version ==
  str(DocumentVersion.version_number)`; ingestion must write exactly these.
- Final ordering: reranked text first, then page-image records (fusion-score
  order) capped at `max_page_image_results=2`; tune mix on M1.
- Neighbor expansion appends after `final_results` with continuing ranks,
  authorized with the same predicate; default off.
- Reranker budget enforced with a conservative chars/4 guard; production
  should use the MiniLM tokenizer (noted in code).
- Code-query heuristic deliberately excludes `return` ("return policy" is
  prose); explicit `task_hint` always wins.

### Test report
- `pytest services/retrieval/tests/`: **25 passed**, 0 failed.
- During development: 3 test-expectation bugs fixed (RRF arithmetic used the
  annotated ranks; backtick heuristic needed single-backtick; duplicate
  kwarg) and 1 real gap fixed (embedding width was checked against the
  client's self-declared dim instead of the configured contract width —
  now both must equal the configured width or the search fails closed).

### Not done / gaps
- No live Milvus/Postgres/GPU here: `VectorStore`, `SqlAlchemyCatalogPort`,
  `EmbeddingClient`, and `Reranker` are interfaces + fakes; the pymilvus
  adapter, EmbeddingGemma 2 CPU server, and MiniLM loader are sibling /
  deploy-time work.
- `main.py` HMAC header verification is implemented locally against the
  gateway's documented `X-Druid-*` contract — confirm byte-exact payload
  format with the gateway sibling before wiring (`wire()`).
- Matryoshka widths are coded for 768 only; a validated width change plugs
  into `RetrievalConfig.embedding_dimension` + re-embedding per the
  migration rule.
- ACLs narrower than group-wide are not yet modeled in `AuthPredicate`
  (spec allows group-wide for v1; `check_permission` integration is a
  follow-up).

## Ingestion workers — 2026-10-08 (subagent build)

Scope: `services/ingestion/` per spec v2.1 §5/§6/§7/§8/§12. Plugged into
the sibling agents' work: catalog models/repository/activation
(`services/catalog/`), `RequestContext` (`services/common/`), RQ `ingest` +
`delete` queues (matches the ingestion Dockerfile CMD).

### Files created
- `__init__.py` — public surface re-exports.
- `config.py` — `IngestionConfig`/`RouterThresholds`/`ChunkerSettings`/
  `OlmOcrSettings`/`EmbeddingSettings`; every router threshold named,
  documented, and marked PROVISIONAL for M1 calibration; env overrides
  (`ROUTER_*`, `CHUNK_*`, `PDFTEXT_WORKERS`).
- `router.py` — `route_page()` → NATIVE/VISUAL/SKIP_BLANK on pure signal
  functions: text density (chars/in²), replacement-char ratio, private-use
  ratio, extraction-vs-geometric order disagreement, column clustering,
  table signals; `signals_to_dict()` for parsing diagnostics.
- `tables.py` — `detect_table_regions()` from line geometry (repeated row
  pitch, columnar whitespace gutters, grid alignment) with 0..1 confidence
  + named evidence; overlapping candidates merged.
- `native_worker.py` — `extract_pages_native()` (pdftext `workers=`
  process pool; documents the per-worker page threshold +
  `PDFTEXT_WORKER_PAGE_THRESHOLD`), `extract_tables_native()`,
  `render_page_png()` (1288px longest dim). pdftext/pypdfium2 imported
  lazily; module-level hard rule: processes only, never threads.
- `olmocr_client.py` — vLLM `/v1/chat/completions` client for
  `allenai/olmOCR-2-7B-1025-FP8`; prompts built ONLY via
  `olmocr.prompts` toolkit builders (fail-closed ImportError, never
  hand-rolled); frontmatter parse; quality checks (rotation validity,
  non-trivial output, repetition guard); retries → `quality_status=review`.
- `tree.py` — `Node`/`NodeType` canonical tree, `stable_node_id()`
  (content-derived, retry-idempotent), `build_tree_from_native()`
  (font-size heading inference, native tables with `native-detected`
  provenance), `build_tree_from_olmocr()` (markdown → typed nodes,
  `olmocr-routed` table provenance), `tree_to_markdown()` (simple → pipe
  tables, complex → JSON, never lossy), `to_dict`/`from_dict`,
  `collect_diagnostics()`.
- `chunker.py` — `StructuralChunker`: prose 420/48 with paragraph-boundary
  overlap (prose only), tables split by row groups with repeated headers
  (rows never duplicated via overlap), code split at blank lines, figures
  as render-reference chunks; **hard rule** `token_len ≤ 512 −
  max_query_tokens` asserted per chunk (violations raise, never truncate);
  tokenizer injected (production: pre-staged MiniLM via
  `MINILM_TOKENIZER_PATH`, fail-closed); `to_milvus_record()` shapes the
  §9 record incl. `identifiers` and modality metadata.
- `identifiers.py` — `IdentifierExtractor` with configurable patterns
  (part numbers, invoice/order numbers, clause/section refs, document
  numbers); deduped, order-preserving, capped.
- `embeddings.py` — `EmbeddingBackend` ABC; `PrefixContract` with the
  contractual pairs (`title: {heading} | text: {chunk}`,
  `task: question answering | query: {q}`, code-retrieval pair for code,
  search pair as approved alternative); `build_document_text()` selects
  the code pair for CodeBlock chunks; page-image records
  (`modality="page_image"`, page label as text, never OCR);
  `SentenceTransformersGemmaBackend` (CPU, text+image config, audio
  disabled, local-path-only + fail-closed, Matryoshka truncation guarded
  by validated widths); `MockEmbeddingBackend` (deterministic, normalized).
- `pipeline.py` — `run_ingestion_job()`: rebuilds `RequestContext` from the
  job's persisted trusted fields, `revalidate_job_context()` fail-closed,
  stage machine PARSING→CHUNKING→EMBEDDING→INDEXING with per-stage commits,
  low-confidence table pages rerouted to visual, olmOCR failures →
  `quarantine_page()` → REVIEW_REQUIRED (parked, never retried blindly),
  activation via `catalog.activation.activate_version()` (range check +
  quarantine gate), failures → RETRY_PENDING/FAILED with attempt counting;
  `run_deletion_job()` (catalog soft-delete → index sink → object prefix
  delete → DELETED); `enqueue_ingestion_job()` (catalog job +
  RQ `ingest` queue); seams `IndexSink`/`LoggingIndexSink`, `ObjectStore`/
  `MemoryObjectStore`/`MinIOObjectStore`.
- `requirements.txt` — added pinned `sentence-transformers==3.3.1`,
  `olmocr==0.4.0` (prompt builders only). Regenerate with pip-compile
  before release.
- `README.md`, `tests/` (6 modules, 51 tests).

### Test results
- `python3 -m pytest services/ingestion/tests/ -q` → **51 passed**.
- Covers: router signals/decisions incl. tunable thresholds; table-region
  scoring on synthetic grids; tree building + markdown round-trip + stable
  IDs; chunker hard budget rule on adversarial inputs (giant prose, wide
  tables, huge code), header repetition, no row duplication via overlap;
  prefix contract per content type; identifier extraction; state machine
  happy path/retries/review-parking/activation-range-gate/deletion
  against real catalog models on SQLite.
- Sibling suites unaffected: `services/catalog/tests/` → 16 passed.
  `services/gateway/tests/` cannot collect here (missing `jwt` package —
  pre-existing env issue, unrelated to this change).

### Decisions made
- Mixed native+visual documents get `parser="mixed"` on the version (needs
  its own `ParserRevisionStats` row for the activation range check).
- `_ReviewRequired` is control-flow, not an error: caught explicitly in
  `_run`, never retried.
- Native pages do not get page-image records by default
  (`page_images_for_native_pages=False`); M1 tunes the modality mix.
- Tokenizer is injected; tests use a word counter, production uses the
  pre-staged MiniLM tokenizer — the budget *logic* is tested, the real
  tokenizer wiring is a deploy-time check (see gaps).

### Not done / gaps
- No live pdftext/vLLM/Redis/MinIO here: native extraction, olmOCR calls,
  RQ enqueue, and MinIO I/O are coded but untested against real services;
  needs integration tests on the H200 host.
- `sentence-transformers` / `olmocr` pins not verified by a real
  pip-compile run; Docker build will confirm.
- Router thresholds are provisional — M1 eval corpus calibration required
  before the ≥95% routing-accuracy gate (§5).
- Real MiniLM tokenizer budget check needs `MINILM_TOKENIZER_PATH`
  staged; the word-counter tests prove the splitting logic, not the exact
  token counts.
- `IndexSink` real implementation is the Milvus agent's scope;
  `LoggingIndexSink` only stages records.
- Page-image records for native pages need the on-demand render path
  (flagged, default off).

---

## Milvus setup + cross-service integration — 2026-10-08 (subagent)

### Built: `services/milvus_setup/`

pymilvus (2.5.6, verified against the installed wheel) translation of spec §9 (v2.1):

- **`schema.py`** — single source of truth as plain data (`FIELD_SPECS`,
  `BM25_FUNCTION_SPEC`, `INDEX_SPECS`) plus lazy `build_pymilvus_schema()` /
  `build_index_params()` translators. Collection `project_knowledge_base_v1`,
  alias `project_knowledge_base`, `group_id` partition key. All 13 §9 fields:
  `chunk_id` PK (opaque-ID `^[A-Za-z0-9_-]{1,128}$`), `group_id`,
  `document_id`, `document_version`, `parent_id` (nullable), `page_start` /
  `page_end`, `chunk_order`, analyzer-enabled `text` (65535, standard
  tokenizer), `identifiers` ARRAY[VARCHAR], `dense_vector` FLOAT_VECTOR 768
  (unified text/page-image space), `sparse_vector` SPARSE_FLOAT_VECTOR,
  `metadata` JSON (incl. `modality`). BM25 function `text_to_bm25`
  (`text`→`sparse_vector`); indexes HNSW/COSINE (`M=16`, `efConstruction=200`)
  + `SPARSE_INVERTED_INDEX`/BM25 (not IP). `partitionkey.isolation` off by
  default, `--enable-partition-isolation` flag only (spec §9: test-first,
  never the auth boundary).
- **`create_collection.py`** — `python -m milvus_setup.create_collection`
  (matches the fixed Dockerfile CMD). Idempotent: connect-with-retry (compose
  does not health-gate this job), create-if-missing, schema-compatibility
  verify on existing collections (fail closed with cutover guidance — never
  auto-migrates), ensure missing indexes, ensure loaded, ensure alias.
  `--check-only`, `--recreate --yes` (data loss, guarded), `--set-alias
  COLLECTION --yes` (cutover/rollback per §8 migration rule).
- **`deletion.py`** — tenant-scoped delete through the alias:
  `group_id == "<t>" and document_id == "<d>"`; IDs validated against the
  opaque-ID contract, `ValueError` instead of interpolation on mismatch.
  Documents the compaction visibility window (§12) — retrieval must still
  apply the catalog hard filter.
- **`README.md`** — usage, cutover/rollback, deletion contract.
- **`tests/`** — 15 pass / 4 skipped (live-Milvus tests skip without
  `DRUID_LIVE_MILVUS=1`).

### Cross-service mismatches found and fixed

1. **Gateway upstream DNS names used hyphens; compose services use
   underscores** (`services/gateway/app.py`). Defaults were
   `http://document-api:8001` / `http://mcp-server:8002` — unresolvable on
   the compose network. Fixed to `http://document_api:8001` /
   `http://mcp_server:8002`. (Gateway's own tests pass explicit URLs, so no
   test changes needed.)
2. **`services.common` not shipped in service images.** catalog, gateway,
   (and future document_api/ingestion/retrieval) code does
   `from services.common.request_context import RequestContext`, but the
   gateway Dockerfile copied only `services/gateway/`, and
   document_api/ingestion/retrieval Dockerfiles did
   `pip install /tmp/catalog` — which fails outright (no `pyproject.toml` /
   `setup.py` in `services/catalog/`). Fixed all five Dockerfiles to copy
   the services tree (`services/__init__.py` + `services/common/` +
   `services/catalog/`) into `/app/services/`; removed the broken
   `pip install /tmp/catalog` lines. **Sibling builders: service code must
   use `from services.catalog...` / `from services.common...` imports.**
3. **Catalog one-shot image couldn't run Alembic.** `alembic/env.py` does
   `from services.catalog.models import Base` but the image copied files
   flat into `/app`. Restructured the catalog Dockerfile to preserve the
   tree (`/app/services/...`, `WORKDIR /app/services/catalog`,
   `CMD ["alembic", "upgrade", "head"]`).
4. **Host port collision on the single H200 host.** `.env.example` set
   `GATEWAY_PORT=8000` and `VLLM_PORT=8000` — both compose projects bind the
   host port. Changed `GATEWAY_PORT` to `8080` (container still listens on
   8000; only the host mapping moved). Covered by a new deploy-wiring test.
5. **Missing env template entries.** Added `DOCUMENT_API_URL`,
   `MCP_SERVER_URL`, `INTERNAL_SECRET` to `.env.example` (gateway's
   `GatewaySettings` reads all three; the BUILD_LOG flagged them as
   coordinator-owned). Also fixed `druid_internal.py` docstring, which said
   `DRUID_INTERNAL_SECRET` — the real name is `INTERNAL_SECRET`.
6. **mcp_server crash-loops in compose.** `create_app()` raises RuntimeError
   without real service clients or `DRUID_MCP_DEV_FAKE_CLIENTS=1`; compose
   set neither. Added `DRUID_MCP_DEV_FAKE_CLIENTS: "1"` to the compose
   `mcp_server` service with a TODO to remove once real HTTP clients land
   (sibling scope).
7. **pydantic pin conflict (2.10.6 vs 2.11.7) — real, not cosmetic.**
   mcp_server pins 2.11.7; everything else pinned 2.10.6. Root-caused: mcp
   1.12.0's `Tool.from_function` does `issubclass(param.annotation,
   Context)` on (string, via `from __future__ import annotations`)
   annotations; pydantic 2.11's `ModelMetaclass` gained a lenient
   `__subclasscheck__` (returns False instead of raising TypeError), 2.10
   lacks it — so mcp_server's contract tests hard-fail under 2.10.6.
   Verified all other services' suites still pass under 2.11.7, then unified
   every `requirements.txt` to `pydantic==2.11.7`.
8. **sqlalchemy-family drift where code is shared.** document_api/ingestion
   pinned sqlalchemy 2.0.37 / alembic 1.14.1 / psycopg 3.2.5 while the
   catalog code they now copy pins 2.0.38 / 1.15.2 / 3.2.9 (install-verified
   by the catalog builder). Aligned the consumers to the catalog pins.
   retrieval's requirements lacked sqlalchemy/psycopg entirely despite
   copying catalog code — added both.
9. **milvus_setup Dockerfile CMD didn't match the code.** CMD was
   `python -m create_collection` but the module uses package-relative
   imports. Fixed to copy the package tree and run
   `python -m milvus_setup.create_collection` (verified in a simulated
   `/app` layout: `--help` works, `--recreate` without `--yes` fails
   closed).
10. **pymilvus 2.5.6 API corrections** (found while building against the
    real wheel): `IndexParams` lives at `pymilvus.milvus_client.index`
    (not top-level); `MilvusClient.create_index(collection, index_params)`
    takes an IndexParams object, not per-field kwargs;
    `CollectionSchema.partition_key_field` returns the FieldSchema, not the
    name string.

### Integration test suite: `tests/` (build root)

46 passed / 13 skipped (`pytest tests/`, shared `.venv` at build root):

- `test_imports.py` — every built module imports cleanly (22 modules);
  document_api/embedding/ingestion/retrieval report as skips until their
  builders land code (the test fails loudly if code appears without import
  coverage).
- `test_app_factories.py` — gateway `create_app()` serves `/healthz`;
  module-level `app` exists for uvicorn; MCP `create_app()` builds with
  fake clients and refuses without JWT.
- `test_gateway_mcp_seam.py` — end-to-end identity across the hop with a
  faked httpx layer: token → trusted context, spoofed `X-Druid-*` headers
  stripped, caller-supplied `group_id` in tool args ignored, forwarded
  headers carry a verifiable HMAC signature, tampered signature rejected,
  unauthenticated → 401 with nothing forwarded.
- `test_catalog_seam.py` — SQLite slice: only READY active versions are
  searchable; cross-tenant fetch raises NotFoundError; tenant-b sees zero
  searchable versions.
- `test_retrieval_seam.py` — contract test for the unbuilt retrieval
  service: chunk contract fields, §8 v2.1 task-aware prefix pairs (Q&A
  default, code pair for code), tenant predicate on dense/lexical/identifier
  branches, fail-closed without tenant, deterministic RRF, provenance-tagged
  `Passage` rendering with `trust="untrusted-document-content"` and
  modality markers (injected "instruction" text stays inert inside
  delimiters).
- `test_deploy_wiring.py` — static compose↔Dockerfile↔entrypoint↔env checks:
  every built service's Dockerfile exists, CMDs resolve to real modules,
  services-tree layout present wherever `services.*` is imported, env
  template covers gateway/MCP/milvus_setup settings, no host-port collision
  across the two compose projects, every `${VAR}` has a template entry or
  default.
- `test_live_infra.py` — 8 skipped gates (PostgreSQL ×2, Redis ×2, Milvus
  ×2, vLLM/H200 ×2), each named and documented per §13.

Full-tree totals (suites run separately — see gaps): **191 passed, 17
skipped, 0 failed** (artifacts 21, gateway+catalog 35, milvus_setup 15+4
skip, mcp_server 74, build-root 46+13 skip).

### Decisions made

- Shared build-root `.venv/` (gitignored) with the unified pins for
  integration testing; each service Dockerfile still installs its own
  `requirements.txt`.
- `partitionkey.isolation` stays OFF by default with an explicit opt-in
  flag, per spec §9 test-first posture.
- Deletion helper operates through the alias and validates IDs; the
  compaction visibility window is documented, not papered over.
- Did not restructure sibling test packaging (see gaps).

### Not done / gaps

- **document_api, embedding, ingestion, retrieval have no application code
  yet** (sibling scope). Their Dockerfiles/requirements/compose entries are
  now correct and waiting; their code MUST use `from services.catalog...` /
  `from services.common...` imports (see fix #2).
- **Real MCP downstream HTTP clients** (retrieval/ingestion/catalog) are
  still ABCs + in-memory fakes; compose runs the MCP server with
  `DRUID_MCP_DEV_FAKE_CLIENTS=1` until they land (fix #6).
- **Root `pyproject.toml` `testpaths = ["services"]` cannot run the whole
  tree in one invocation**: `services/mcp_server/tests`,
  `services/artifacts/tests`, etc. all claim the top-level `tests` package
  name (each service dir lacks `__init__.py`), so a single pytest run
  collides with `ImportPathMismatchError`. Suites must be run per-service
  (as each builder did) until test packaging is unified — left for the
  coordinator since it touches siblings' layouts. Build-root `tests/` is
  intentionally NOT a package (`__init__.py` removed) for the same reason.
- Live gates untested here: Alembic against real PostgreSQL, Redis Lua quota
  scripts, Milvus setup/idempotency/alias cutover, vLLM olmOCR smoke +
  GPU-arbitration burn-in (all have named skipped tests).
- MinIO image still unpinned (`minio/minio:latest`, TODO from scaffold).
- Dependency pins still not `pip-compile`d against PyPI (repo policy).
- `eval/` corpus scaffolding (M1) not started.

## 2026-10-08 — embedding service (spec §8)

Built `services/embedding/` as a standalone CPU FastAPI service (port 8003):

- `reuse.py` — loads the ingestion sibling's `services/ingestion/embeddings.py`
  WITHOUT executing `services/ingestion/__init__.py` (which would import pdftext
  and heavy deps). The §8 prefix contract has exactly one home; this service
  reuses it, never duplicates it. A test asserts byte-equality with the
  sibling's formatters.
- `settings.py` — pydantic-settings with `EMBEDDING_` prefix (matches
  `.env.example`); dimension restricted to validated Matryoshka widths
  (768/512/256/128); modality `text+image`|`text-only`; backend `st`|`mock`.
- `service.py` — `EmbeddingService`: server-side prefix application through the
  sibling's contract, batching, semaphore + encode lock for CPU serving,
  first-vector width/normalization verification (fail closed), char-limit
  guard (rejects, never silently truncates), page-image embedding with
  `modality: "page_image"` metadata and page-label text.
- `main.py` — `POST /embed` (documents + queries, task-aware prefixes),
  `POST /embed_image` (b64 PNG → unified-space vector; `object_key` → 501,
  MinIO fetch is deploy-time work), `GET /health` (model card, fail-closed).
  Lifespan warmup forces backend load at startup: missing/invalid artifacts
  fail startup loudly (§3), never download.
- `Dockerfile` — copies the five stdlib-only sibling contract files
  (embeddings/config/chunker/tree/identifiers.py) WITHOUT the ingestion
  `__init__.py`; CPU-only torch; `HF_HUB_OFFLINE=1`.
- `.env.example` — added `EMBEDDING_MODEL_PATH` (primary; `EMBEDDING_MODEL_DIR`
  fallback alias), modality/dimension/backend vars.

Tests: **17/17 pass** (`services/embedding/tests/`): prefix contract per task
type (Q&A default, code-retrieval, search-result), byte-equality with sibling
formatters, page_image metadata shape + label-not-OCR, fail-closed startup
(missing path, missing env), Matryoshka width guard (100 rejected, 256
accepted), normalization, char guard, object_key 501.

Gaps:
- No model weights in this environment: the ST backend is coded against the
  local pre-staged path but never exercised against real EmbeddingGemma 2
  weights — first deploy must run the warmup and confirm resident RAM.
- `object_key` (MinIO fetch) for `/embed_image` is 501; wire at deploy time.
- Real prefix-quality validation happens on the M1 corpus (§8), not here.

## document_api — 2026-10-08 (document + review-queue API, spec §11)

Built `services/document_api/` as a FastAPI service running behind the gateway:

- `main.py` — `create_app()` factory (injectable settings/session_factory/object_store) + module-level `app` for `uvicorn main:app`; error handlers map domain errors to HTTP codes with no detail leakage on 401/403/404.
- `internal_auth.py` — byte-exact mirror of the gateway's `druid_internal.py` signing contract (`druid-v1|user|group|roles_sorted_csv|request_id`, HMAC-SHA256, `v1:<hex>`); fail-closed on missing headers, bad signature, or unconfigured secret. Kept local so the image stays independent of the gateway package.
- `routes/documents.py` — POST /v1/documents/upload (magic-byte MIME sniff, size cap streamed at 1MiB, pypdf trailer page count, sha256, opaque UUID, tenant-scoped MinIO key, commit-before-enqueue, enqueue-failure parks job as FAILED), GET /v1/documents (tenant-scoped + review_required filter), GET /v1/documents/{id} (metadata + versions + presigned artifact refs), DELETE /v1/documents/{id} (authorize → enqueue deletion, idempotent via per-document idempotency key reporting the existing job), GET /v1/documents/{id}/status (active version's ingestion job).
- `routes/review.py` — GET /v1/review/items, GET /v1/review/items/{item_id} (render + parse attempts + diagnostics), POST .../resolve (accept/re-routed/reject; reviewer role required).
- `job_queue.py` — durable job creation + RQ enqueue by dotted-path worker entry points (`services.ingestion.pipeline.run_ingestion_job` / `run_deletion_job`); `test_entrypoints.py` asserts the strings resolve to the real functions. (Named `job_queue.py`, not `queue.py`, to avoid shadowing the stdlib `queue` module in the container.)
- `storage.py` — ObjectStore ABC + MemoryObjectStore + MinIOObjectStore (lazy minio import); tenant-scoped keys `{group_id}/{document_id}/{version_id}/...`.
- `validation.py`, `schemas.py`, `settings.py` (names follow shared .env.example), `deps.py`.
- `Dockerfile`: added `PYTHONPATH=/app` so `uvicorn main:app` resolves reliably.

Tests: **34/34 pass** (`services/document_api/tests/`): HMAC matrix incl. cross-compat with the gateway's own `build_internal_headers` (tampered group/roles rejected), unconfigured-secret fail-closed, upload validation (garbage/MIME-mismatch/empty/413/400 page-limit/filename sanitization), tenant scoping (list/get/status/delete 404 across tenants), deletion enqueue + idempotency, review RBAC (403 without reviewer role, 409 on re-resolve), entry-point resolution.

Catalog additions (shared file, backwards-compatible): `CatalogRepository.list_version_jobs()` and `get_job_by_idempotency_key()` — both tenant-scoped. Catalog suite still 35/35 green.

Gaps / follow-ups:
- `services.ingestion.pipeline.enqueue_ingestion_job` cannot carry the worker-required `pdf_key` payload (it hardcodes `payload={"queue": ...}`); document_api therefore enqueues via its own `job_queue.py`. The pipeline helper should accept a `payload_extra` dict, or the two should be consolidated.
- Upload versions are created with `parser="mixed"`, `parser_revision="router-v1"` (per-page routing decides native vs visual later); `ParserRevisionStats` needs seeding from the M1 corpus before activation range-checks are meaningful.
- Gateway Dockerfile CMD is `uvicorn main:app` but the gateway package ships `app.py` (no `main.py`) — likely a broken container entrypoint; needs owner confirmation.
- Live Redis/MinIO/Postgres paths are coded but untested here (no servers in this environment); MinIO image tag pinning is still TODO per the scaffold agent.

## 2026-10-08 — Post-session fix: filename in retrieval results (Finch)

User asked that retrieval results name the source document (page numbers were
already present). `PassageProvenance` gained a required `filename` field
(document display name); `Passage.render()` now emits `filename="..."` inside
the provenance delimiters (attribute-escaped like the other fields). The
retrieval service already resolved filename per hit — only the MCP
provenance model was dropping it. Test added:
`test_render_includes_filename_and_pages`. mcp_server suite: 75/75 pass.

---

## 2026-10-08 — Hardening run (`new-druid-full-build-attempt`, manually triggered by James)

Continue-mode: no rebuild from scratch. Test sweep via subagent + wiring
audit + dependency re-pin by Finch. Final: **343 passed, 12 skipped, 0
failed** — unified `pytest services/` (273 passed / 4 skipped) plus
build-root `tests/` (70 passed / 8 skipped).

### 1. Test sweep (subagent) — all green after 2 stale-test fixes
- `tests/test_deploy_wiring.py::test_gateway_cmd_matches_module` asserted
  `"main:app"` — stale from before the hand fix; updated to `"app:app"`.
- `tests/test_retrieval_seam.py` constructed `PassageProvenance` without the
  required `filename` field (added 2026-10-08); fixture + assertion updated.
- Hand fixes verified: gateway Dockerfile CMD is `uvicorn app:app`;
  `enqueue_ingestion_job(..., payload_extra=None)` present and merged.
- Observation (left for this run, fixed below): `test_imports.py`'s
  `test_service_code_not_yet_built` never fired (dotted names used as paths).

### 2. CRITICAL dependency re-pin — EmbeddingGemma 2 (Finch)
`sentence-transformers==3.3.1/3.4.1` + `transformers==4.48.3` could not
load the released `google/embeddinggemma-2` checkpoint (needs ST>=6.1.0,
transformers 5.x per its `config_sentence_transformers.json`).
- **embedding**: ST==6.1.0, transformers==5.18.0 (checkpoint built with
  5.18.0.dev0), torch stays ==2.6.0, huggingface-hub 0.27.1→==1.33.0
  (ST 6.1.0 requires >=1.3.0). pip-compile resolves the full closure.
- **ingestion**: ST==6.1.0, transformers==5.18.0 added, torch==2.6.0 pinned
  (matches embedding). pip-compile resolves the full closure.
- Two **phantom pins** found by pip-compile (never published to PyPI):
  `pdftext==0.4.2` → `==0.4.1`; `olmocr==0.4.0` → `==0.4.27`
  (0.4.2's unconditional `transformers==4.55.2` conflicts with the 5.x
  requirement; 0.4.27 keeps transformers under the `gpu` extra only, and
  its `build_no_anchoring_v4_yaml_prompt` is byte-identical to 0.4.2's —
  verified by source diff). `pypdfium2` aligned 4.30.1→4.30.0 (pdftext
  0.4.1 pins it exactly).
- Code API check: `SentenceTransformer(path, device="cpu",
  trust_remote_code=False, config_kwargs={...})` and
  `.encode(..., normalize_embeddings=, show_progress_bar=)` are valid
  against ST 6.1.0 (comment in `embeddings.py` records this).
- **PrefixContract prompt strings untouched** (verified verbatim 2026-10-08).
- Embedding + ingestion suites re-run post-pin: **68/68 pass**.
- Stale comment fixed: `embedding/settings.py` no longer claims 270M/440M
  encoders (points at the manifest's single-744M-checkpoint reality).

### 3. Cross-service wiring fixes (Finch)
- **ingestion Dockerfile — two real bugs fixed:**
  (a) exec-form `CMD ["rq", "worker", "--url", "${REDIS_URL}", ...]` never
  expands `${REDIS_URL}` (Docker does not expand env vars in exec form) —
  switched to shell form; (b) flat `COPY services/ingestion/ /app/` broke
  the RQ dotted-path entry points (`services.ingestion.pipeline.*`) the
  document API enqueues — now copies the full services tree with
  `PYTHONPATH=/app` (verified by importing both entry points in a
  simulated image layout). Also added the CPU-index torch two-step
  (mirrors the embedding service; CUDA torch must never land here).
- **ingestion → vLLM was unwired:** `olmocr_client` defaulted to
  `http://olmocr-vllm:8000` (no such service; real name is `vllm-olmocr`)
  and the CPU/GPU compose projects shared no network (separate default
  networks → no cross-project DNS). Fixed: default URL →
  `http://vllm-olmocr:8000`; new shared external `druid-net` network in
  both compose files, attached to `ingestion` and `vllm-olmocr`;
  `OLMOCR_VLLM_URL` added to `.env.example` (replacing the dead
  `VLLM_HOST` var nothing read); runbook gained the
  `docker network create druid-net` step-0.
- **`.env.example` gained `MINILM_TOKENIZER_PATH`** (read by
  `ingestion/chunker.py::_default_token_len`, previously untemplated).
- **New build-root `.dockerignore`**: keeps `.venv/` (document_api's is
  90 MB and was COPY'd into its image), `__pycache__/`, `tests/`,
  `.env`, `.git/` out of image build contexts.
- uvicorn CWD behavior verified empirically: `--app-dir` defaults to
  `""` (= CWD on sys.path), so `uvicorn app:app` / `main:app` resolve in
  the gateway/retrieval/embedding images. No changes needed there.
- 5 new deploy-wiring regression tests (ingestion tree layout, no
  unexpanded `${...}` in exec-form CMD, `.dockerignore`, shared
  vLLM network). Wiring suite: 15/15 pass.

### 4. Test packaging unified — one command runs the whole tree
- Root cause of the old `ImportPathMismatchError`: every suite dir is
  named `tests`. Fixed by making each a real package
  (`services/<svc>/tests/__init__.py` + missing service-level
  `__init__.py` for artifacts/embedding/mcp_server) so modules import as
  `services.<svc>.tests.test_*` (unique). Build-root `conftest.py`
  documents the three sys.path entries (build root,
  `services/mcp_server/src`, `services/document_api/`).
- Gotchas found and fixed along the way: `services/mcp_server/pyproject.toml`
  has its own `[tool.pytest.ini_options]` (`asyncio_mode=auto`), which
  re-roots pytest and drops the root conftest for mcp-only targets —
  unified runs use the build-root ini, so `asyncio_mode=auto` was added
  there too (14 unmarked async mcp tests relied on it); `pypdf` installed
  into the build-root `.venv` (document_api tests need it).
- `python -m pytest` from the build root now runs all 273 service tests
  in ~7s. Per-service runs still work unchanged.

### 5. pip-compile verification (repo policy)
All 9 services' pin sets resolve to consistent closures via pip-compile
(pip-tools 7.6.2, PyPI metadata); headers in each `requirements.txt`
record the 2026-10-08 verification. Locked files must still be
regenerated on the 3.11 image target before release (this VM is 3.12).

### 6. Not done / gap list (needs James, his H200, or later work)
- **Spec §8 correction (flagged, NOT rewritten):** §8 claims a quantized
  sub-1 GB footprint and independently-loadable 270M/440M/300M encoders.
  The released repo is a single 744M BF16 checkpoint (~1.49 GB on disk,
  ~3 GB+ RAM at serving). Needs his sign-off before the spec is edited.
- **Six §15 spec decisions** still unanswered — v2.1 defaults in use.
- **MinIO image tag** still `minio/minio:latest` (Docker Hub API
  unreachable from this environment; pin a dated RELEASE tag at deploy).
- **M1 eval-corpus construction** (needs his documents); router thresholds
  stay provisional until the ≥95% routing-accuracy gate.
- **Live-infra integration tests**: Alembic vs real PostgreSQL, Redis Lua
  quota scripts, Milvus setup/idempotency/alias cutover, vLLM olmOCR
  smoke + GPU-arbitration burn-in, real EmbeddingGemma 2 backend warmup
  (ST backend coded but never run against weights), real MiniLM tokenizer
  budget check. All have named skipped tests.
- **Real MCP downstream HTTP clients** (retrieval/ingestion/catalog) still
  ABCs + fakes; compose runs MCP with `DRUID_MCP_DEV_FAKE_CLIENTS=1`.
- **No GitHub push** (needs his explicit approval); no model downloads.
- vLLM healthcheck uses `curl` — confirm the `vllm/vllm-openai:v0.7.3`
  image ships it, at deploy time.

---

## 2026-10-09 — Logging audit + gap-fill (all non-GPU services, per James's request)

House style: `log = logging.getLogger(__name__)` per module, %-style format
args, INFO/WARNING/ERROR/DEBUG, `logger.exception` in handlers. (Existing
modules kept their names — e.g. `druid.ingestion`, `retrieval`,
`milvus_setup` — not renamed.) No shared `get_logger()` helper was added:
two lines per module matched the house pattern and a helper would not have
reduced duplication. Stdlib only, no new dependencies, no unrelated
reformats. GPU serving code (vLLM/olmOCR server side) untouched — only the
HTTP *client* (`ingestion/olmocr_client.py`) was instrumented.

### Per-service changes

- **gateway** (`app.py`, `proxy.py`, `auth.py`): startup banner at
  `create_app()` (upstreams, quota-store backend, quota limits, forward
  timeout, JWT algs). Auth failures → 401 logged at WARNING (no token
  contents). Quota rejections → 429 logged at INFO with group, quota kind,
  scope, remaining, request_id, path. Upload 413/400 rejections at WARNING.
  Invalid MCP JSON at WARNING. `proxy.forward_request`: INFO start/finish
  with method/path/upstream/request_id/group_id + duration_ms; upstream
  timeout → 504 and transport error → 502 at WARNING. JWKS fetch failure
  at WARNING in `auth.py`.
- **document_api** (`main.py`, `routes/documents.py`, `routes/review.py`,
  `job_queue.py`): lifespan startup banner (queues, object-store backend,
  bucket, upload limits, redis/database configured flags — values, never
  secrets). Error handlers log 401/403/409 at WARNING, 404 at INFO with
  path + request_id. Upload: INFO start (filename, group, request_id) +
  finish (document_id, version_id, job_id, page_count, duration_ms);
  validation/auth rejections at WARNING; enqueue failure at ERROR with
  job_id; defensive except → `log.exception`; best-effort orphan-cleanup
  failures now logged at DEBUG (were silent `pass`). Delete: INFO
  start/finish with document/job IDs, dedup-on-conflict at INFO, enqueue
  failure at ERROR. Review resolve: INFO business event (item, decision,
  reviewed_by, group, request_id); 403/404/409 at WARNING/INFO.
  `enqueue_rq`: INFO per enqueued job (job_id, type, queue, entrypoint).
- **catalog** (`activation.py`, `repository.py`): no entrypoint (library).
  `activate_version`: INFO at start and on completion (version → READY,
  document, group, deactivated-previous count); every rejection path now
  logs at WARNING via a `_reject` helper (quarantine, status, chunk-count,
  missing baseline, deleted document). Repository business events at INFO:
  `create_job`, `soft_delete_document`, `quarantine_page`,
  `resolve_quarantine_item` (IDs + tenant context).
- **mcp_server** (`server.py`, `tools.py`, `stdio_adapter.py`): startup
  banner in `create_app()` (JWT algs/issuer/audience, origin-allowlist
  count, dev-fake-clients flag — never secrets). `AuthMiddleware`:
  WARNING on origin rejection / missing-or-malformed auth / bad token;
  INFO per-request finish (method, path, status, duration_ms, group,
  request_id). `_require` wrapper: INFO tool start/finish with tool name,
  user, group, request_id, duration_ms; `log.exception` on tool failure.
  stdio adapter: startup banner (user, group, roles) with
  `logging.basicConfig(..., stream=sys.stderr)` — stdout stays
  protocol-clean for the MCP transport.
- **ingestion**: `pipeline.py` already logged transitions; added worker
  entry INFO in `run_ingestion_job` / `run_deletion_job` (job_id).
  `olmocr_client.py` (vLLM HTTP client — in scope): per-attempt WARNING on
  request failure and quality rejection (page_index, attempt n/max,
  reason); WARNING when a page is parked for review; INFO on accepted
  (attempts, latency). `native_worker`, `router`, `chunker`, `tables`,
  `tree`, `embeddings`, `config` needed nothing: their excepts all raise
  (ImportError→RuntimeError, env-parse defaults) — no silent paths.
- **embedding** (`main.py` already had the startup banner; `service.py`
  already logged): `/embed` and `/embed_image` now log INFO start
  (counts, task / page_label) and finish (vector count, duration_ms);
  400s at WARNING, 500s at ERROR, the 501 object_key branch at WARNING.
- **retrieval**: `service.search()` INFO start (group, request_id,
  query_chars) and finish (result count, duration_ms); INFO when the
  page-image reranker-bypass fallback triggers (count); INFO when
  catalog reconciliation drops mid-search deactivations. `main.wire()`
  startup banner (embedding dim, candidate widths, expand_neighbors);
  invalid-JSON 400 at WARNING with request_id; `catalog_port` logs the
  hard-filter query failure at WARNING before raising DependencyError.
  Unhandled-exception handler already used `log.exception`.
- **milvus_setup**: `create_collection.py` already well logged; added a
  startup banner in `main()` (uri, collection, alias, timeout, argv).
  `deletion.delete_document`: INFO start/finish with group, document,
  alias, deleted count.
- **artifacts** (`cli.py`): `print()` diagnostics converted to logging
  (INFO/ WARNING/ERROR, `basicConfig` in `main()` + startup banner with
  command + manifest). The `OK:` / `INVALID:` / `FAILED:` verdict lines
  stay on stdout/stderr as the CLI result payload (Docker ENTRYPOINT
  contract) — verified: diagnostics on stderr, `OK:` on stdout, exit 0.

### Verification

- Grep audit: zero bare `except:` and zero `except ...: pass` remain in
  `services/`; `print(` remains only in the artifacts CLI verdict lines
  (intentional payload) and one test fixture string.
- `python -m pytest` from the build root: **273 passed, 4 skipped**
  (skips are pre-existing live-Milvus tests in
  `services/milvus_setup/tests/test_setup_live.py` — need
  `DRUID_LIVE_MILVUS=1` + a real Milvus; not runnable on this VM).
- Per-service: gateway 19, document_api 34, catalog 16, ingestion 51,
  embedding 17, retrieval 25, milvus_setup 15 (+4 skipped), artifacts 21,
  mcp_server 75 — all green.
- Smoke-tested: gateway app import (banner fires), document_api
  `create_app()`, artifacts CLI `validate` on
  `manifests/olmocr-2-7b-1025-fp8.yaml` (stderr diagnostics, stdout
  `OK:`, exit 0).

### Deliberately left alone

- vLLM/olmOCR *serving* code: the build tree contains no serving code for
  it (only the `OlmOcrClient` HTTP client, which was instrumented); the
  H200 GPU path has its own logging per the task.
- `document_api/validation.py`, `storage.py`, `deps.py`,
  `internal_auth.py`, `gateway/druid_internal.py`, `quotas.py` stores,
  `mcp_server/auth.py` + `clients.py`, `catalog/models.py` + alembic
  migrations, `retrieval/reranker.py`, `milvus_setup/schema.py`,
  `artifacts/artifact_manifest.py`: pure helpers, schema, or raise-loud
  code — no silent failure paths, so no logging added (failures surface
  through the instrumented callers).

## 2026-10-09 — New service: web_ui (spec §16, v2.2)

Tenant document-management UI. FastAPI + Jinja2 + htmx 2.0.4 (vendored at
`services/web_ui/static/htmx.min.js` — no CDN, offline-first). Standalone
service: talks ONLY to the gateway with the user's own Bearer <redacted> (`POST
/v1/documents/upload`, `/v1/documents*`, `/v1/review/*`, `POST /mcp` →
`search_knowledge_base`). No direct Postgres/Milvus/MinIO access.

Auth: `auth.py` is a byte-mirror of `services/gateway/auth.py` (verified by
diff — docstring differs only). Login page accepts a pasted provisioned JWT,
validates it, stores it in HttpOnly/Secure/SameSite cookie. Identity comes
from the token only; spoofable headers stripped by middleware; fail closed.
Roles from token claims: `user`, `reviewer` (review actions; admins always
can), `admin` (operator views). Admin is never grantable through the UI.

Pages: Upload (drag-and-drop, chunked/resumable via /upload/session* — the
browser splits files, `complete` assembles and forwards ONE multipart upload;
gateway enforces authoritative limits), Documents (enriched with active-version
status/page count, htmx 15s polling, version history, delete w/ confirm),
Review queue (list + detail with page render/parse attempts/diagnostics;
accept/re-route/reject gated on reviewer role, backend enforces again),
Quarantine (FAILED docs), Search (provenance-tagged passages w/ filename +
page range), Admin: all-documents, jobs, quotas.

Deviations/notes:
- Internal port 8005, not 8003 (8003=embedding, 8004=retrieval). Compose
  service + `WEB_UI_PORT`/`GATEWAY_URL` added to deploy/docker-compose.yml
  and .env.example.
- Admin pages call PROPOSED `/v1/admin/*` document_api endpoints (documents,
  jobs, quotas) — these do NOT exist downstream yet; pages degrade gracefully
  with an explanatory notice. Exact proposed contract documented in
  services/web_ui/README.md. Follow-up needed in document_api.
- No credential store exists in new_DRUID: login is token-paste; tokens are
  provisioned out-of-band by the operator (documented in README).

Tests: 32 passed, 0 failed (services/web_ui/.venv). Tenant isolation proven
against a per-token mocked gateway: spoofed X-Group-Id/X-Druid-* headers and
query params ignored, tenant A cannot see/reach tenant B's documents,
non-admin → 403 on all admin views, roles only from token claims, chunked
upload round-trip with out-of-order resume and byte-identical assembly,
search provenance rendering.

## 2026-10-09 — Admin API: /v1/admin/{documents,jobs,quotas} implemented (Finch)

The web_ui admin pages called three document_api endpoints that did not
exist (README marked the contract "proposed"). All three are now implemented
in document_api, reachable via the gateway's /v1/{path:path} proxy:

- `routes/admin.py` (new): `GET /v1/admin/documents` (tenant + status
  filters, pagination; rows carry document_id, display_name, group_id,
  latest-version status, updated_at), `GET /v1/admin/jobs` (tenant filter;
  rows carry job_id, group_id, job_type, state, attempt_count, error excerpt
  truncated to 500 chars, document_id, created_at, updated_at,
  computed duration_seconds), `GET /v1/admin/quotas` (per-tenant
  ingest_pages_used/limit and query_qps/limit). All require the `admin`
  role in JWT claims (403 otherwise); unauthenticated → 401. Tenant filter
  is an explicit `tenant` query param (tenant name = group_id claim), never
  a header. Wired into main.py.
- `services/catalog/repository.py`: new `list_all_documents`,
  `list_all_jobs`, `list_tenant_names` — deliberately bypass the tenant
  predicate; docstrings require callers to enforce the admin role.
- `document_api/settings.py`: `quota_ingest_pages_per_day` (1000) and
  `quota_query_qps` (10) mirror the gateway's QUOTA_* env vars for limit
  reporting. `deps.py`: new `get_redis_client` (lazy redis import, matching
  the job_queue.py convention — the test venv has no redis package).
- Quotas read the gateway's own Redis counters
  (`druid:quota:ingest:{tenant}:{date}`, `druid:quota:query:{tenant}`; key
  scheme documented in `_quota_keys`, must stay in sync with
  gateway/quotas.py). Redis unreachable → usage reported as null, not zero.
- Contract deltas vs the web_ui README proposal (documented in the web_ui
  README): added `status` filter on documents (latest-version state, not
  active-version — fresh uploads have no active version yet); jobs gained
  document_id/created_at/duration_seconds; NO `progress` field (jobs table
  has no progress column; state + attempt_count are the signal).
- Tests: new `tests/test_admin.py` — 15 tests (cross-tenant listing, tenant
  + status filters, 400 on bad status, 403 for non-admin/reviewer, 401
  unauthenticated, quota shape/limits, Redis-down → null usage).
  document_api suite: 49 passed (34 existing + 15 new). Catalog suite:
  16 passed. web_ui suite: 32 passed (README/api_client comments updated
  from "proposed" to implemented).

---

## 2026-10-09 — Follow-ups: port fix, real MCP HTTP clients (Finch)

### 1. Host-port collision fix
- `deploy/docker-compose.yml`: gateway host default `${GATEWAY_PORT:-8000}` →
  `${GATEWAY_PORT:-8080}`, matching `.env.example` (was inconsistent; vLLM keeps
  host 8000). Compose-DNS/container ports untouched.
- `tests/test_deploy_wiring.py`: new `test_no_host_port_collision_within_compose_files`
  and `test_no_host_port_collision_across_druid_and_sage` — parse `ports:` from
  druid-cpu, druid-gpu, and New_sage compose files, resolve `${VAR:-default}` via
  each project's `.env.example`, fail on any duplicate host port. Sage tree
  auto-discovered at `~/workspace/new_sage/build` (`NEW_SAGE_BUILD_ROOT` override);
  skips (not fails) when absent so the repo stays self-contained. Includes a
  negative test proving an injected collision is detected.
- Also fixed a pre-existing docs bug found while here: `docs-site` smoke test had
  the gateway check at `localhost:8000/healthz` instead of `8080`.

### 2. Real MCP downstream HTTP clients
- New `services/mcp_server/src/mcp_server/http_clients.py`: httpx-based
  `HttpRetrievalClient`, `HttpCatalogClient`, `HttpIngestionClient` honoring the
  ABC signatures; 404 → existing `NotFoundError`, other failures → new
  `ServiceClientError` (the ABC defines no exception for those cases). Internal
  identity headers signed byte-exact per each downstream verifier (verified
  against `document_api.internal_auth` and `retrieval.main` verifiers).
- No direct catalog HTTP API exists — catalog reads go through document_api's
  existing endpoints (nothing invented): `list_documents` → `GET /v1/documents`
  + bounded per-item detail enrichment (status filters other than
  REVIEW_REQUIRED applied client-side; documented approximation);
  `get_review_item` → `GET /v1/review/items/{id}` (opaque storage keys, never
  presigned URLs); `resolve_review_item` → `POST …/resolve` (accept→accepted,
  reroute→re-routed, reject→rejected); `delete_document` → `DELETE
  /v1/documents/{id}`.
- Gaps (documented in code + README): `register_upload` → NotImplementedError
  (no ticket-issuing endpoint; document_api upload is single multipart with
  bytes the MCP tool never holds — fix: add `POST /v1/documents/register` or
  change the ABC); `get_job_status(job_id)` → NotImplementedError (no
  job-scoped endpoint; only per-document status — fix: add tenant-scoped
  `GET /v1/jobs/{job_id}` or change the ABC).
- `server.py`: real clients by default; fakes only behind
  `DRUID_MCP_DEV_FAKE_CLIENTS=1`. New settings: `retrieval_url`,
  `document_api_url`, `internal_secret`, `downstream_timeout_seconds`.
- `deploy/docker-compose.yml`: removed `DRUID_MCP_DEV_FAKE_CLIENTS=1` from
  mcp_server; `depends_on` now `[document_api, retrieval]`. `.env.example`:
  added `RETRIEVAL_URL=http://retrieval:8004`, `MCP_DOWNSTREAM_TIMEOUT_SECONDS=30`.
- Tests: `services/mcp_server/tests/test_http_clients.py` — 31 tests with
  `httpx.MockTransport` (success + error paths per method). mcp_server suite:
  106 passed (75 existing + 31 new). Build-root suite: 319 passed, 4 skipped
  (`--ignore=services/web_ui`; web_ui collection error is pre-existing —
  standalone web_ui suite still 32/32).

## 2026-10-09 — MinIO → RustFS swap (James-approved; local only, not pushed)

MinIO community edition is dead (repo archived, Docker Hub repos deleted).
Swapped the object-store backend to RustFS `rustfs/rustfs:1.0.1` (pinned),
per his standing call from the port/minio review. The `minio` Python client
is kept unchanged — it works unmodified against any S3-compatible backend.

- `deploy/docker-compose.yml`: `minio` service replaced with `rustfs`
  (`--console-address ":9001"`, env `RUSTFS_ACCESS_KEY`/`RUSTFS_SECRET_KEY`,
  named volume `rustfsdata:/data`, healthcheck on `http://localhost:9000/health`).
  Milvus keeps its required `MINIO_*` names but now points at
  `rustfs:9000` / `${RUSTFS_ACCESS_KEY}` / `${RUSTFS_SECRET_KEY}`; `depends_on`
  and the "shares OUR MinIO" comment updated; stale "pin dated RELEASE tag"
  TODO deleted with the old service.
- `.env.example`: `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` →
  `RUSTFS_ACCESS_KEY=druid`/`RUSTFS_SECRET_KEY=change-me-min-8-chars`,
  `RUSTFS_ENDPOINT=rustfs:9000`, `RUSTFS_BUCKET=druid-artifacts`.
- `services/document_api/`: `settings.py` — `object_store` default `"rustfs"`,
  `rustfs_endpoint`/`rustfs_access_key` (`AliasChoices("RUSTFS_ACCESS_KEY")`) /
  `rustfs_secret_key` (`AliasChoices("RUSTFS_SECRET_KEY")`) / `rustfs_bucket` /
  `rustfs_secure` / `rustfs_presign_expiry_seconds`; no legacy MINIO_* aliases
  (clean break — not deployed yet). `storage.py` — `MinIOObjectStore` →
  `RustFSObjectStore` (class, `__all__`, error message, docstring). `deps.py`,
  `main.py` (startup banner), `routes/documents.py`, `routes/review.py` updated
  to the `rustfs_*` attributes.
- `services/ingestion/pipeline.py`: `MinIOObjectStore` → `RustFSObjectStore`
  (class, `__all__`, error message); "MinIO in production" docstrings →
  "RustFS in production"; `'MinIO key of the original'` → `'S3 key of the original'`.
  Same S3-key wording applied to `document_api/job_queue.py`.
- Remaining comment/README mentions swept in `catalog/models.py`,
  `ingestion/embeddings.py`, `ingestion/README.md`, `document_api/README.md`,
  `embedding/main.py` + `README.md`, `web_ui/{api_client.py,main.py,README.md}`.
- Tests: `services/web_ui/tests/conftest.py` and
  `services/mcp_server/tests/test_http_clients.py` fake presigned URLs
  `http://minio/...` → `http://rustfs/...`.
- Docs (living only): `docs/runbook.md` (bring-up command now
  `up -d postgres redis rustfs etcd milvus`; sizing-table row renamed),
  `docs/backup-restore.md` (`mc mirror` → `rclone sync` to an S3 remote,
  service names minio→rustfs). `docs/architecture-spec-v2.1.md` untouched
  (versioned snapshot).
- Verification: `docker` is not installed in this VM, so
  `docker compose config` could not be run; compose YAML was validated by
  parsing with PyYAML (service/env/volume/depends_on all as intended).
  Tests: build-root suite 319 passed, 4 skipped (`--ignore=services/web_ui`;
  pre-existing web_ui collection issue, see above) + standalone web_ui suite
  32 passed — zero failures from the rename.
- Not in scope (left for a later pass): `docs/architecture-spec-v2.1.md`
  history entries, `skills/druid-sage/references/{deployment,troubleshooting}.md`
  MinIO mentions, and build-root `README.md` MinIO lines — all flagged, none changed.

## 2026-10-09 — Docs/skill sync for MinIO→RustFS swap (Finch)

- `~/workspace/druid-sage-docs/src/`: all MinIO references updated to RustFS
  (`druid-deployment.md`, `druid-architecture.md`, `druid-configuration.md`,
  `druid-operations.md`, `druid-troubleshooting.md` — incl. a new "Object
  storage: RustFS (replaced MinIO)" note, `druid-web-ui.md`,
  `druid-api-reference.md`). Site rebuilt: link check 378/378 pass.
- `~/workspace/skills/druid-sage/references/`: `deployment.md`,
  `troubleshooting.md` updated; re-synced into `build/skills/druid-sage/`.
- `build/docs-site/` re-synced from `~/workspace/druid-sage-docs/` (39 files).
- `docs/architecture-spec-v2.1.md` deliberately untouched (versioned snapshot).
