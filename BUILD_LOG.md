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
