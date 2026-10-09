# new_DRUID — API Reference (Document & Admin)

All document/admin API calls go through the **gateway**, which
authenticates the caller and forwards with HMAC-signed identity headers.
The routes below are copied from `services/document_api/routes/`
(`documents.py`, `review.py`, `admin.py`); nothing else is documented
here.

## Auth model

1. **Client → gateway.** Every request carries
   `Authorization: Bearer <JWT>`. The gateway validates signature,
   issuer, audience, and expiry (HS256 shared secret or RS256 via JWKS).
   The trusted context (`user_id`, `group_id`, `roles`, `request_id`) is
   built from token claims **only**; `group_id` is required, never
   inferred from headers.
2. **Gateway → document_api.** The gateway strips spoofable headers and
   signs the identity into `X-Druid-*` headers: `X-Druid-User`,
   `X-Druid-Group`, `X-Druid-Roles`, `X-Druid-Request-Id`, plus
   `X-Druid-Internal-Sig`. The signature is HMAC-SHA256 over
   `druid-v1|user|group|roles_sorted_csv|request_id`, formatted as
   `v1:<hex>`, keyed by `INTERNAL_SECRET`. document_api verifies the
   signature byte-exactly and **fails closed** on missing headers, bad
   signature, or an unconfigured secret.

## Tenant scoping

The `group_id` claim is resolved to a tenant row; every repository query
is filtered to that tenant. Cross-tenant IDs are indistinguishable from
missing ones: the API returns **404**, never a permission hint.
Filenames are display metadata only and never become storage paths;
object keys are tenant-scoped
(`{group_id}/{document_id}/{version_id}/...`).

## Document routes

### `POST /v1/documents/upload` → 201

Multipart upload. Server-side: magic-byte MIME sniff, size cap streamed
at 1 MiB, PDF trailer page count, sha256, opaque UUIDs, tenant-scoped
RustFS key, commit-before-enqueue, enqueue failure parks the job as
`FAILED`.

Response `UploadResponse`:

```json
{
  "document_id": "…",
  "job_id": "…",
  "status": "UPLOADED",
  "page_count": 42,
  "checksum_sha256": "…",
  "display_name": "contract.pdf"
}
```

Limits: `MAX_UPLOAD_MB` (default 200) → **413**; `MAX_PAGES_PER_DOCUMENT`
(default 2000) → **400**. Allowed MIME types: `application/pdf`,
`image/png`, `image/jpeg`, `image/tiff`.

### `POST /v1/documents/register` → 201

Two-phase upload, step 1: declare the upload (display name, MIME, byte
size, SHA-256). Server validates the declaration (name sanitizing, MIME
allowlist, size cap → **413**, SHA-256 shape → **400**), creates the
document record + durable ingestion job in `UPLOADED` (not yet enqueued —
no bytes exist), and issues a **single-use upload ticket** (TTL
`upload_ticket_ttl_seconds`, default 900s). The ticket is redeemed in
step 2; it is the authorization the MCP `ingest_document` tool returns.

### `POST /v1/documents/{document_id}/bytes` → 200

Two-phase upload, step 2: redeem the ticket and send the bytes (multipart,
ticket as form field). Constant-time ticket compare, TTL check, single-use:
wrong ticket → **403**, consumed → **409**, expired → **410**. Bytes must
match the declared size and SHA-256 exactly (**400** otherwise); sniffed
MIME must agree with the declared one. From version creation onward the
flow mirrors the single-shot upload (tenant-scoped RustFS key,
commit-before-enqueue, enqueue failure parks the job `FAILED`).

### `GET /v1/documents`

Tenant-scoped list. Query params:

| Param | Notes |
|---|---|
| `review_required` | `true` filters to documents awaiting reviewer attention (quarantine surface) |

### `GET /v1/documents/{document_id}`

Metadata + versions + presigned artifact URLs for one document.

### `DELETE /v1/documents/{document_id}` → 202

Authorizes in the catalog, then enqueues complete deletion (Milvus
chunks, originals, derived artifacts, renders, caches, catalog
visibility). **Idempotent:** retried DELETEs report the already-queued
job instead of duplicating work. The job is not `DELETED` until every
store confirms.

Response `DeleteResponse`: `{document_id, job_id, status}`.

### `GET /v1/documents/{document_id}/status`

Job status for the document's **active version** ingestion job:
`{job_id, job_type, state, document_id, document_version_id}`.

## Job routes

### `GET /v1/jobs/{job_id}` → 200

Status of any job by id, tenant-scoped (cross-tenant reads as **404**,
never 403). Reuses the job status shape: `{job_id, job_type, state,
attempt_count, error}` with the error excerpt truncated to 500 chars.
Unknown job → **404**. Unauthenticated → **401**.

## Review routes

### `GET /v1/review/items`

List quarantine items for the tenant. Query params:

| Param | Default | Notes |
|---|---|---|
| `status` | `pending` | One of `pending`, `accepted`, `re-routed`, `rejected`; anything else → **400** |
| `limit` | 100 | 1–500 |
| `offset` | 0 | ≥ 0 |

### `GET /v1/review/items/{item_id}`

Returns the quarantine item: page render (presigned URL +
`page_render_key`), both parse attempts, routing diagnostics, and any
reviewer decision. 404 unless owned by the tenant.

### `POST /v1/review/items/{item_id}/resolve`

Applies **accept / re-route / reject**. Body: `{decision, detail?}`.
Requires the **reviewer** role — anything else → **403**. Re-resolving
an already-resolved item → **409**.

## Admin routes (admin role required)

All three require the `admin` role in the caller's JWT claims
(non-admin → **403**, unauthenticated → **401**). The tenant filter is
an explicit `tenant` query param (the tenant name = `group_id` claim),
never a header. These are reachable via the gateway's `/v1/{path}` proxy.

### `GET /v1/admin/documents`

Query params: `tenant` (filter), `status` (filter on the document's
**latest** version ingestion state, e.g. `UPLOADED`, `READY`; invalid
values → **400**), `limit`, `offset`. Rows carry `document_id`,
`display_name`, `group_id`, latest-version `status`, `updated_at`.

### `GET /v1/admin/jobs`

Query params: `tenant` (filter), `limit`, `offset`. Rows carry `job_id`,
`group_id`, `job_type`, `state`, `attempt_count`, `error` (excerpt,
truncated to 500 chars), `document_id`, `created_at`, `updated_at`,
computed `duration_seconds` (`updated_at − created_at`). No `progress`
field exists — the `jobs` table has no progress column; `state` +
`attempt_count` are the progress signal.

### `GET /v1/admin/quotas`

Per-tenant `{group_id, ingest_pages_used, ingest_pages_limit,
query_qps, query_qps_limit}`. Usage reads the gateway's Redis counters
(`druid:quota:ingest:{tenant}:{date}`, `druid:quota:query:{tenant}`);
limits mirror the gateway's `QUOTA_INGEST_PAGES_PER_DAY` /
`QUOTA_QUERY_QPS` env vars. Redis unreachable → usage is **null**, not
zero.

## Error codes

| Code | Meaning | Where |
|---|---|---|
| 400 | Invalid parameter (`status` value, `page_count`, filename, MIME mismatch, empty file) | document_api |
| 401 | Missing/malformed Bearer token, invalid JWT (signature, issuer, audience, expiry), missing `group_id` claim; or bad gateway HMAC signature on internal hops | gateway, document_api |
| 403 | Authenticated but lacking the required role (reviewer for review resolve, admin for `/v1/admin/*`); spoofed/missing internal identity | gateway, document_api |
| 404 | Unknown, foreign, or cross-tenant document/job/item (cross-tenant is 404, not 403) | document_api |
| 409 | Conflict: job already queued with the same idempotency key; quarantine item already resolved | document_api |
| 413 | Upload exceeds `MAX_UPLOAD_MB` | gateway |
| 422 | Pydantic validation of MCP tool arguments (tool contracts) | mcp_server |
| 429 | Quota exhausted, with `Retry-After` (rounded up, min 1s) | gateway |
| 500 | Unhandled error (logged with `log.exception`) | all services |
| 502 / 504 | Upstream transport error / timeout from the gateway proxy | gateway |

Error handlers never leak detail on 401/403/404.

## MCP server endpoint

The MCP API is separate from the REST routes above: Streamable HTTP at
`/mcp` on port 8002, bearer-JWT authenticated. See the [MCP tools
reference](druid-mcp-tools.html) for all 8 tools.
