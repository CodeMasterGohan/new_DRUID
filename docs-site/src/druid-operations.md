# new_DRUID — Operations

## Backup & restore

A single H200 is a single point of failure. Backups are load-bearing, and
**restore is tested on a schedule, not just documented.**

### What gets backed up

| Store | Tool | Scope |
|---|---|---|
| Milvus | `milvus-backup` | `project_knowledge_base_v*` collections + aliases |
| PostgreSQL | `pg_basebackup` + WAL archiving (or nightly `pg_dump`) | Full catalog: tenants, documents, versions, jobs, review quarantine |
| MinIO | `mc mirror` to a second MinIO / filesystem target | Originals, page renders, parsed trees, markdown, provenance |

Back up the **catalog and Milvus together** (same window): the catalog's
active-version pointers must be consistent with the indexed collections.
A chunk without a catalog record is garbage; a catalog record without its
chunks is a hole the hard filter will correctly hide — but restore should
not rely on that.

### Schedule (initial)

- Milvus: nightly `milvus-backup create`, retain 7 days.
- PostgreSQL: continuous WAL archiving + weekly base backup, retain 4 weeks.
- MinIO: continuous `mc mirror --watch`, plus nightly snapshot, retain 30 days.

### Restore procedure

1. Stop all services except the infrastructure (`postgres`, `minio`, `milvus`).
2. Restore PostgreSQL from base backup + WAL to the backup timestamp T.
3. Restore MinIO objects to T.
4. Restore Milvus collections with `milvus-backup restore` to T.
5. Run `milvus_setup` in **verify mode** (must confirm, not recreate):
   the alias `project_knowledge_base` resolves and the collection schema
   matches §9.
6. Run the catalog/Milvus **reconciliation check**: every `READY`
   document version must have its chunks present; every chunk's
   `document_version` must resolve to an active catalog record.
   Quarantine mismatches to `REVIEW_REQUIRED` — never silently expose them.
7. Bring services up; run the §13 smoke queries (known-answer set from M1).

### Restore testing

- Full restore drill **monthly** to an isolated namespace/host.
- Pass/fail: the system must reach queryable state and pass the M1
  known-answer spot checks.
- Record each drill (date, duration, issues) — a drill that isn't
  recorded didn't happen.

### RTO / RPO targets (initial, tune after first drill)

- RPO ≤ 24 h (nightly Milvus/PG backups; MinIO continuous).
- RTO ≤ 4 h for full restore on equivalent hardware.

### Deletion interplay (§12)

Backups retain deleted data until retention expires. The tenant-facing
retention policy must say this plainly: "deleted" means invisible to
retrieval immediately (catalog hard filter), physically gone from live
stores after compaction, and gone from backups after the retention window.

## Quotas

Enforced at the gateway (spec §4); the MCP server does not duplicate
enforcement.

| Quota | Default env | Semantics |
|---|---|---|
| Ingest pages/day | `QUOTA_INGEST_PAGES_PER_DAY=1000` | Per-tenant daily page budget |
| Query QPS | `QUOTA_QUERY_QPS=10` | Per-tenant sliding-window queries/sec |
| Max upload size | `MAX_UPLOAD_MB=200` | → 413 over cap |
| Max pages/document | `MAX_PAGES_PER_DOCUMENT=2000` | → 400 over cap |

- **Quota classification:** `POST /v1/documents/upload` and MCP
  `tools/call → ingest_document` consume ingestion quota (`page_count`
  from query/form/arguments); everything else counts against query QPS.
  Tool arguments influence only the quota class, never identity.
- **Storage:** `RedisQuotaStore` (production) with atomic Lua scripts —
  ingestion consumption rolls back on rejection so accounting stays
  exact. `InMemoryQuotaStore` for tests/dev.
- **Redis keys:** `druid:quota:ingest:{tenant}:{date}` (daily counter),
  `druid:quota:query:{tenant}` (1-second sliding-window sorted set). The
  document_api admin quotas page reads the same keys; they must stay in
  sync with `gateway/quotas.py`.
- **Exhaustion:** HTTP 429 with `Retry-After` (rounded up, min 1s).
  Quota rejections are logged at INFO with group, quota kind, scope,
  remaining, request_id, and path.

## Monitoring & logging

House style (audited 2026-10-09): `log = logging.getLogger(__name__)` per
module, %-style format args, `INFO` / `WARNING` / `ERROR` / `DEBUG`,
`logger.exception` in handlers. No bare `except:` and no
`except ...: pass` remain anywhere in `services/`. Zero new dependencies
(stdlib logging only).

What gets logged, per service:

- **gateway** — startup banner (upstreams, quota-store backend, quota
  limits, JWT algs); auth failures → 401 at WARNING (no token contents);
  quota rejections → 429 at INFO; upload 413/400 rejections at WARNING;
  per-request forward INFO start/finish (method, path, upstream,
  request_id, group, duration_ms); upstream timeout → 504, transport
  error → 502 at WARNING; JWKS fetch failure at WARNING.
- **document_api** — lifespan startup banner (queues, object-store
  backend, bucket, upload limits — values, never secrets); error handlers
  log 401/403/409 at WARNING, 404 at INFO; upload INFO start/finish
  (filename, document_id, version_id, job_id, page_count, duration_ms),
  validation/auth rejections at WARNING, enqueue failure at ERROR with
  job_id; delete INFO start/finish + dedup-on-conflict at INFO; review
  resolve INFO business event (item, decision, reviewed_by, group,
  request_id); per-enqueue INFO in `enqueue_rq` (job_id, type, queue,
  entrypoint).
- **catalog** (library, no entrypoint) — `activate_version` INFO at start
  and completion; every rejection path (quarantine, status, chunk-count,
  missing baseline, deleted document) at WARNING; repository business
  events (`create_job`, `soft_delete_document`, `quarantine_page`,
  `resolve_quarantine_item`) at INFO with IDs + tenant context.
- **mcp_server** — startup banner (JWT algs/issuer/audience,
  origin-allowlist count, dev-fake-clients flag — never secrets); auth
  middleware WARNING on origin rejection / missing-or-malformed auth /
  bad token; INFO per-request finish (method, path, status,
  duration_ms, group, request_id); the `_require` wrapper logs INFO tool
  start/finish (tool name, user, group, request_id, duration_ms) and
  `log.exception` on tool failure. stdio adapter logs to **stderr** —
  stdout stays protocol-clean for the MCP transport.
- **ingestion** — worker entry INFO (`run_ingestion_job` /
  `run_deletion_job` with job_id); per-stage transitions in the pipeline;
  olmOCR client: WARNING per-attempt on request failure and quality
  rejection (page_index, attempt n/max, reason), WARNING when a page is
  parked for review, INFO on accepted (attempts, latency).
- **embedding** — startup banner; `/embed` and `/embed_image` INFO start
  (counts, task / page_label) and finish (vector count, duration_ms);
  400s at WARNING, 500s at ERROR, the 501 `object_key` branch at WARNING.
- **retrieval** — `service.search()` INFO start (group, request_id,
  query_chars) and finish (result count, duration_ms); INFO on
  page-image reranker-bypass fallback and on catalog reconciliation
  dropping mid-search deactivations; `main.wire()` startup banner
  (embedding dim, candidate widths, neighbor expansion); invalid-JSON
  400 at WARNING; catalog-port hard-filter failure at WARNING before
  raising `DependencyError`.
- **milvus_setup** — startup banner (uri, collection, alias, timeout,
  argv); `delete_document` INFO start/finish (group, document, alias,
  deleted count).
- **artifacts CLI** — diagnostics to logging (stderr); the `OK:` /
  `INVALID:` / `FAILED:` verdict lines stay on stdout as the CLI result
  payload (Docker ENTRYPOINT contract).

Secrets and token contents are never logged. Presigned artifact URLs are
generated at read time, never logged.

## Job queue

- **Broker:** Redis (`REDIS_URL`), via **RQ** (`rq` chosen for
  simplicity; swappable). Redis also serves as the quota store and cache.
- **Queues:** `ingest` and `delete`.
- **Entry points** (dotted strings resolved at worker time):
  `services.ingestion.pipeline.run_ingestion_job`,
  `services.ingestion.pipeline.run_deletion_job`.
- **Enqueue discipline:** the document API creates the durable catalog
  job row first, then enqueues via `enqueue_rq` with
  `job_id=f"{queue}-{job.id}"` — **commit-before-enqueue**, so a worker
  can never race the caller's uncommitted transaction. Enqueue failure
  parks the job as `FAILED` (logged at ERROR with the job id).
- **Payloads:** ingestion —
  `{"queue", "pdf_key", "content_type", "page_count", ...}`;
  deletion — `{"queue", "document_id"}`.
- **Workers:** RQ workers run inside the scalable `ingestion` compose
  service (`docker compose up -d --scale ingestion=4 ingestion`).
  Ingestion workers rebuild the trusted `RequestContext` from the job's
  persisted trusted fields and revalidate it fail-closed; the ingestion
  state machine (PARSING → CHUNKING → EMBEDDING → INDEXING) commits per
  stage, with failures → `RETRY_PENDING` / `FAILED` and attempt counting.
- **Deletion:** catalog soft-delete → index sink → object prefix delete
  → `DELETED`; the job is not `DELETED` until every store confirms.
