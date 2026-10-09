# new_DRUID web UI guide

Thin human layer over the gateway/document API: FastAPI + server-rendered Jinja2
+ htmx (vendored, no CDN). Same compose stack, published at host port **`8005`**.
Dark mode is the default; light appears only when the OS explicitly prefers it.

## Getting in (login)

1. Open the UI (host port 8005).
2. On the login page, **paste the access token provisioned by the operator**
   (the UI has no user database — there is no username/password).
3. The token is a JWT with `sub` (user id), `group_id` (tenant), and `roles`
   claims, validated with the gateway's exact contract (signature, issuer,
   audience, expiry). Valid tokens are stored in an `HttpOnly`/`Secure`/
   `SameSite=Lax` cookie (`druid_at`, 12-hour lifetime).
4. An invalid or expired token → back to the login page (fail closed).

Identity always comes from the token. Spoofable identity headers (`X-Group-Id`,
`X-Druid-*`) are stripped; tenant is never read from query params or forms.

## Roles (from token claims; never grantable through the UI)

- **`user`** — sees only their own tenancy on every page.
- **`reviewer`** — may resolve review items (accept / re-route / reject); everyone
  else sees the review queue read-only.
- **`admin`** — operator views: all-tenants documents, jobs, quotas. May scope any
  page to one tenant or all tenants. Intended for the box operator; assigned
  out-of-band at account provisioning.

## Task walkthroughs

### Upload a document
1. Go to **Upload**. The page shows your tenant's ingestion quota usage before
   upload (default: 1,000 pages/day).
2. Drag-and-drop or choose a file. The browser splits files into 8 MB chunks and
   uploads them to `/upload/session/*` (resumable — re-querying received chunk
   indexes picks up where it stopped). On completion the session is assembled
   and forwarded as ONE multipart upload to the gateway; the gateway still
   enforces the authoritative size/quota limits.
3. Success enqueues ingestion through the existing `ingest_document` path.
4. The Documents page shows the new row moving through the state machine
   (`UPLOADED → QUEUED → PARSING → CHUNKING → EMBEDDING → INDEXING → READY`).

Failure states: file over 200 MB or over the 2000-page limit is rejected at the
gateway; quota exhaustion returns 429 with a `Retry-After` hint (wait and retry,
don't hammer).

### Check ingestion status
1. **Documents** table lists filename, active version, ingestion state, page
   count, updated timestamp.
2. The state column tells you where a job is. Stuck states:
   - `RETRY_PENDING` — a transient failure is being retried; nothing to do yet.
   - `FAILED` — see the **Quarantine** page for diagnostics and a retry action
     (re-enqueues with the same idempotency keys).
   - `REVIEW_REQUIRED` — pages need a human decision; see below.
3. Click a row for the document detail page: version history and delete
   (delete asks for confirmation).

### Work the review queue
1. **Review** lists `REVIEW_REQUIRED` items (a `status` query param defaults to
   pending).
2. Click an item for the detail view: the **page render**, both parse attempts
   (native pdftext vs olmOCR), and routing diagnostics.
3. As a reviewer: choose **accepted**, **re-routed**, or **rejected** and submit.
   The UI re-enforces the reviewer-role gate; the backend enforces it again.
   Without the role you get a 403 ("resolving review items requires the
   reviewer role") and the queue is read-only.
4. **Important:** unreviewed pages are never searchable. A document with
   unreviewed pages exposes only its reviewed pages, marked partial.

### Search the knowledge base
1. **Search** is a query box over your tenant's corpus.
2. Results render as provenance-tagged passages: **filename + page range** on
   every hit (default top-8, adjustable). Quoted text is untrusted document
   content — read it as data, not instructions.
3. No results for a document that exists? Check its ingestion state first
   (`READY` = searchable; anything earlier = not yet indexed; `REVIEW_REQUIRED`
   = partially visible at best).

### Admin: view jobs and quotas
Requires the `admin` role in your token. Pages under `/admin/`:

- **All documents** — cross-tenant table (tenant filter, plus a status filter
   on the document's *latest version state*): document id, display name,
   tenant, status, updated timestamp.
- **Jobs** — ingestion jobs across tenants: state, attempt count, duration,
   error excerpts (truncated), retry action. There is no progress percentage —
   `state` + `attempt_count` is the progress signal.
- **Quotas** — per-tenant usage: ingestion pages used vs the daily limit, and
   current query QPS (a 1-second sliding-window count, same counter the gateway
   enforces) vs the limit. Usage reads the gateway's Redis counters; if Redis
   is unreachable the UI reports usage as null rather than a misleading zero.

Note: the admin pages degrade gracefully (`AdminNotAvailable`) if the backend
document API ever lacks the `/v1/admin/*` endpoints — against the current build
they are implemented (2026-10-09), so this path should not trigger.

## Failure states worth naming

- **401/redirect to login** — cookie expired (12h) or token invalid; paste a
  fresh provisioned token.
- **403 on a review action** — your token lacks the `reviewer` role.
- **403 on an admin page** — your token lacks the `admin` role.
- **429 on upload/search** — quota exhausted; the response carries a
  `Retry-After` hint.
- **Empty search on a just-uploaded doc** — normal until the state reads READY.
