# Troubleshooting

Only issues with a source basis (build logs, runbook, code) appear here. Each
entry: symptom → likely cause → fix. Items marked **not-yet-implemented** are
planned-but-missing, not bugs.

## new_DRUID

### Upload fails or is rejected
- **413 / size error** → file exceeds the 200 MB upload cap or the 2000-page
  document limit (gateway-enforced). → Split the document or request a limit
  change (limits are env/config values, not per-file overrides).
- **429 with `Retry-After`** → tenant ingestion quota exhausted (1,000
  pages/day default). → Wait out the window; do not retry blindly. If the
  default is wrong for the workload, validate with a quota burn-in before
  changing it (still an open decision in the spec).
- **Chunked UI upload stalls mid-file** → resume re-queries received chunk
  indexes; if the session expired (1h in-process TTL), start a fresh session.

### Document stuck in a non-READY state
- **`RETRY_PENDING`** → transient failure, retry in progress. Wait; check the
  job row for the error excerpt.
- **`FAILED`** → open **Quarantine** in the UI (or `list_documents` with
  `status: "FAILED"`), read the diagnostics, then retry (re-enqueues with the
  same idempotency keys — no duplicates).
- **`REVIEW_REQUIRED`** → pages need a reviewer decision; nothing is broken.
  Work the review queue (`accepted` / `re-routed` / `rejected`; reviewer role
  required). Unreviewed pages stay unsearchable by design.

### Search returns nothing (or misses a known document)
- Document not yet `READY` → normal; ingestion hasn't finished indexing.
- Document is `REVIEW_REQUIRED` → only reviewed pages are searchable, marked
  partial.
- Deleted/deleting/inactive versions are hard-filtered at the catalog layer —
  they never appear in results even if Milvus compaction hasn't caught up.

### Auth problems (UI or MCP)
- **401 / bounced to login** → token missing, invalid, or expired (12h UI
  cookie). → Paste a freshly provisioned token. Tokens are provisioned
  out-of-band by the operator — there is no self-service reset.
- **403 on a review action** → token lacks the `reviewer` role.
- **403 on `/admin/*`** → token lacks the `admin` role. (Admin is never
  grantable through the UI; roles come from token claims only.)
- **Origin mismatch 403 on MCP** → the MCP server enforces an Origin allowlist
  (`MCP_ALLOWED_ORIGINS`); the caller's Origin must be listed.

### MCP mutating tool "did nothing"
- `resolve_review_item` / `delete_document` with `confirm: false` return a
  confirmation-required response and change nothing. → Re-invoke with
  `confirm: true`.

### Service won't start / crashes at boot
- **"Missing artifact" / model-load failure** → the checkpoint isn't staged at
  the expected `$MODEL_DIR` path or its manifest is incomplete. → Complete the
  manifest (pinned revision, sha256, license) and stage the files; never work
  around it by allowing downloads — offline-first is load-bearing.
- **Any attempt to download at runtime** → a bug. Production services must
  fail startup rather than fetch from upstream.
- **vLLM fails to start on the H200** → check the FP8 checkpoint is the one
  mounted (`allenai/olmOCR-2-7B-1025-FP8`); the BF16 checkpoint is for
  fine-tuning only. Lower `--gpu-memory-utilization` if KV-cache pressure
  appears under concurrent page renders.

### Admin pages show a "not available" notice
- The UI degrades gracefully (`AdminNotAvailable`) when the document API
  behind the gateway doesn't implement `/v1/admin/*`. Against the current
  build (implemented 2026-10-09) this should not trigger — if it does, the
  document_api image is stale.

### Quota page shows null usage
- Expected when Redis is unreachable: the UI reports null rather than a
  misleading zero. → Fix Redis connectivity; do not treat null as "no usage".

## New_sage

### `get-library-docs` returns `version_fallback: true`
- The requested version isn't indexed; the tool served the closest indexed
  version (named in the `version` field). → Either use the served version or
  request a crawl of the missing version.

### `crawl_recommended: true`
- **Not-yet-implemented as an automatic behavior** (as built): it's a flag on
  the response, not an enqueue action. On-demand crawl is the P3 milestone;
  until it lands, a human must add/index the library version.

### DevDocs-based library won't refresh
- **Not-yet-implemented:** the DevDocs image-manifest parser is a stub
  (`NotImplementedError`). The monthly DevDocs sync cannot run until it's
  built.

### Repo-cloned library shows no `commit_sha` tracking
- **Known gap:** commit SHAs have no catalog column (needs an Alembic
  migration). Pinning still works via the tag used for `git clone --branch`.

### `resolve-library-id` returns `found: false` with alternatives
- The name matched nothing exactly but fuzzy candidates exist. → Pick from
  `alternatives` (they carry scores) or add the `ecosystem` hint
  (`pypi` / `npm` / `crates`) — e.g. `requests` collides across ecosystems.

### Unknown `library_id` on `get-library-docs`
- The ID isn't in the resolver table (seed table of 15 libraries at build
  time). The tool says so plainly with empty passages — do not retry the same
  ID; resolve the name first.

### Empty passages but `status` mentions the retrieval service
- The retrieval service is down; the tool degrades gracefully instead of
  failing. → Check the retrieval service health (New_sage retrieval listens
  internally on 8001), not the MCP server.

## Both projects — what "tests green" doesn't cover

The builds were tested green on the build VM (2026-10-08/09), but these were
**not** verified there and will bite at deploy time:
- Docker images were never built (no Docker on the build VM).
- Dependency pins are initial, not pip-compile-verified (new_DRUID scaffold;
  mcp_server pins were verified for that service only).
- MinIO image tag is `latest` with a TODO to pin a dated RELEASE tag.
- No live-infra run on the H200 yet: model staging, full corpus ingestion,
  offline-egress test, quota burn-in, and GPU-arbitration measurement are all
  still ahead.
