# new_DRUID — Web UI User Guide

A thin human layer over the gateway/document_api: FastAPI + Jinja2
server-rendered templates + **htmx 2.0.4 vendored** in the image (no CDN,
per the offline network policy). The UI talks **only** to the gateway
with your own Bearer `<redacted>` — no direct Postgres, Milvus, or RustFS
access. Tenant scoping is enforced downstream; the UI adds no new data
paths.

- **Port:** 8005 (host mapping; container listens on 8005).
- **Theme:** dark mode is the default; light applies only when the OS
  explicitly prefers it (`prefers-color-scheme: light`).

## Logging in

There is **no credential store** in new_DRUID. Access tokens are
provisioned out-of-band by the operator: JWTs (HS256 shared secret, or
RS256 via JWKS) carrying `sub` (user id), `group_id` (tenant), and
`roles` (list of strings) claims, using the same `JWT_*` settings as the
gateway.

1. Open the UI and paste your provisioned JWT into the login page.
2. The server validates signature, issuer, audience, and expiry using the
   gateway's exact auth contract, then stores the token in an
   `HttpOnly`/`Secure`/`SameSite=Lax` cookie.
3. Identity comes from the validated token **only**. Spoofable headers
   (`X-Group-Id`, `X-Druid-*`) are stripped; query params and form fields
   are never consulted for identity. Fail closed.
4. Log out with the logout button (`POST /logout`), which clears the
   cookie.

### Roles

Roles come from the token's `roles` claim — nothing else.

| Role | What it unlocks |
|---|---|
| `user` | Every page, scoped to your own tenancy |
| `reviewer` | Review actions (accept / re-route / reject); `admin` always can |
| `admin` | Operator views (all tenants: documents, jobs, quotas) |

**The `admin` role is never grantable through the UI** — it is assigned
out-of-band at account provisioning (in production, this is James as the
box operator). Non-admins get 403 on all admin views.

## Pages

### Upload — `/upload`

Drag-and-drop upload with **chunked, resumable** large-file support:

- The browser splits the file into chunks (`upload_chunk_mb`, default
  8 MB) and POSTs them to `/upload/session/{id}/chunk`. Resume works by
  re-querying which chunk indexes were already received.
- On `complete`, the UI assembles the bytes and forwards **one** multipart
  upload to the gateway (`POST /v1/documents/upload`). Authoritative
  size and quota limits are enforced at the gateway/document_api, not by
  the browser.
- Upload sessions are in-process with a TTL — correct for the
  single-replica compose deployment.

### Documents — `/documents`

Table of your tenant's documents: filename, active version, ingestion
state, page count, updated timestamp. Details and actions:

- **Detail view** (`/documents/{document_id}`): enriched with the active
  version's status/page count and full **version history**.
- **Delete** (with confirmation): enqueues the §12 deletion (Milvus
  chunks, originals, artifacts, renders, catalog visibility); the job
  reports as already-queued if retried.
- The table refreshes via htmx partials (`/partials/documents/rows`;
  ~15-second polling).

### Review queue — `/review`

Lists `REVIEW_REQUIRED` items (mirrors the
`list_documents?review_required` filter). The detail view
(`/review/{item_id}`) shows the page render, both parse attempts, and
routing diagnostics — mirrors the `get_review_item` MCP tool.

Actions (require the **reviewer** role; backend enforces it again):

- **accept** — the parsed page stands.
- **re-route** — send the page to the other parse path.
- **reject** — drop the page.

Non-reviewers see the queue read-only. Quarantined content is never
searchable.

### Quarantine — `/quarantine`

Documents whose active version is `FAILED`, with diagnostics. The UI
points you back to the upload path: retry is **re-upload** (idempotent on
content checksum) rather than in-place re-enqueue.

> Spec §16 described a retry action here; the built behavior is a
> "Re-upload" button that returns to `/upload`.

### Search — `/search`

A query box over your tenant's corpus. The UI calls `POST /mcp` with
`tools/call → search_knowledge_base` and renders the results as
**provenance-tagged passages** (filename + page range, per the §10
provenance contract). Top-k is adjustable (1–20, default 8).

### Admin views (admin role only)

- **All documents** — `/admin/documents`: cross-tenant document table
  with a tenant filter and a latest-version `status` filter; same actions
  as the tenant view, plus a tenant column.
- **Jobs** — `/admin/jobs`: ingestion/deletion job list across tenants:
  job_id, group_id, job_type, state, attempt_count, error excerpt
  (truncated to 500 chars), document_id, created_at, updated_at,
  computed `duration_seconds`. Note: there is **no `progress` field** —
  the `jobs` table has no progress column; `state` + `attempt_count` are
  the progress signal.
- **Quotas** — `/admin/quotas`: per-tenant quota usage (ingest pages/day,
  query QPS). Usage is read from the gateway's Redis counters
  (`druid:quota:ingest:{tenant}:{date}`,
  `druid:quota:query:{tenant}`); limits mirror the gateway's
  `QUOTA_INGEST_PAGES_PER_DAY` / `QUOTA_QUERY_QPS`. If Redis is
  unreachable, usage is reported as **null**, not zero.

If the document_api admin endpoints are ever missing, the pages degrade
gracefully with an explanatory notice; against the current build the
contract is implemented (2026-10-09).

## What the UI deliberately does not do

- No document editing or annotation — it manages documents, it is not an
  editor.
- No new background jobs — all async work flows through the existing
  Redis ingestion queue; the UI polls job-status endpoints.
- The UI never accepts a tenant/user identifier from the client; the
  two-tenant test (tenant A cannot reach tenant B's documents, jobs, or
  passages) is an explicit acceptance gate.

**Build status:** 32/32 UI tests pass, including tenant-isolation
proofs (spoofed headers ignored, cross-tenant 403s) and a chunked-upload
round-trip with out-of-order resume and byte-identical assembly.

> One deviation from spec §16 worth knowing: the spec put the UI on
> internal port 8003; the build uses **8005** (8003 is the embedding
> service, 8004 the retrieval service). The spec's "login exchanges
> credentials for a token" flow became token-paste, because no credential
> store exists in new_DRUID.
