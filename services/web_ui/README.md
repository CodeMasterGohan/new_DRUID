# web_ui — DRUID tenant document-management UI (spec §16, v2.2)

Thin human layer over the gateway/document_api. FastAPI + Jinja2
server-rendered templates + htmx (vendored at `static/htmx.min.js`,
v2.0.4 — no CDN, per the §3 offline network policy).

## Auth model

- There is **no credential store** in new_DRUID. Access tokens are
  provisioned out-of-band by the operator: HS256 JWTs (or RS256 via JWKS)
  with `sub` (user id), `group_id` (tenant), and `roles` (list of strings)
  claims, using the same `JWT_*` settings as the gateway.
- The login page accepts a pasted token, validates it with the gateway's
  exact contract (`auth.py` is a byte-mirror of `services/gateway/auth.py`),
  and stores it in an `HttpOnly`/`Secure`/`SameSite=Lax` cookie.
- **Identity comes from the validated token only.** Spoofable headers
  (`X-Group-Id`, `X-Druid-*`) are stripped by middleware; query params and
  form fields are never consulted for identity. Fail closed.
- Roles: `user` (own tenancy, every page), `reviewer` (may resolve review
  items; admins always can), `admin` (operator views). **The `admin` role
  is never grantable through the UI** — roles come from token claims only.

## Backend integration

The UI calls **only** gateway endpoints with the user's own Bearer <redacted>
- `POST /v1/documents/upload`, `/v1/documents*`, `/v1/review/*`
  (gateway → document_api, quota-checked, HMAC identity downstream)
- `POST /mcp` with `tools/call` → `search_knowledge_base` (gateway →
  mcp_server) for the Search page

No direct Postgres / Milvus / MinIO access. Tenant scoping is enforced
downstream; the UI adds no new data paths.

## Chunked upload

The browser splits files into `upload_chunk_mb` (default 8 MB) chunks and
POSTs them to `/upload/session/*`. `complete` assembles the bytes and
forwards ONE multipart upload to the gateway (authoritative size/quota
limits still apply there). Resume works by re-querying received chunk
indexes. Sessions are in-process with a TTL — correct for the
single-replica compose deployment.

## Admin contract (IMPLEMENTED in document_api 2026-10-09)

The admin pages call these document_api endpoints via the gateway's
`/v1/{path}` proxy (all require the `admin` role in the caller's token;
non-admin → 403, unauthenticated → 401):

- `GET /v1/admin/documents?tenant=&status=&limit=&offset=` → `{documents: [{document_id, display_name, group_id, status, updated_at}], limit, offset}`
- `GET /v1/admin/jobs?tenant=&limit=&offset=` → `{jobs: [{job_id, group_id, job_type, state, attempt_count, error, document_id, created_at, updated_at, duration_seconds}], limit, offset}`
- `GET /v1/admin/quotas` → `{tenants: [{group_id, ingest_pages_used, ingest_pages_limit, query_qps, query_qps_limit}]}`

Contract deltas vs the original proposal (all additive/superset):

- `GET /v1/admin/documents` accepts an extra `status` query param filtering
  on the document's **latest** version ingestion state (e.g. `UPLOADED`,
  `READY`); invalid values → 400. `status` is the latest version's state
  (a fresh upload is `UPLOADED` long before it has an *active* version).
- `GET /v1/admin/jobs` rows additionally carry `document_id`, `created_at`,
  and computed `duration_seconds` (`updated_at - created_at`); `error` is
  an excerpt truncated to 500 chars. There is **no `progress` field** —
  the `jobs` table has no progress column; `state` + `attempt_count` are
  the progress signal.
- `GET /v1/admin/quotas`: `query_qps` is the tenant's current 1-second
  sliding-window count (same counter the gateway enforces); usage reads
  the gateway's Redis keys (`druid:quota:ingest:{tenant}:{date}`,
  `druid:quota:query:{tenant}`) and reports `null` usage when Redis is
  unreachable rather than a misleading zero. Limits mirror the gateway's
  `QUOTA_INGEST_PAGES_PER_DAY` / `QUOTA_QUERY_QPS` env vars.

The graceful-degradation path (`AdminNotAvailable` on 404) is retained as a
backstop but no longer triggers against the current document_api.

## Configuration (env)

| Var | Default | Notes |
|---|---|---|
| `GATEWAY_URL` | `http://gateway:8000` | compose DNS name |
| `JWT_SECRET` / `JWT_ALGORITHMS` / `JWT_ISSUER` / `JWT_AUDIENCE` / `JWKS_URL` | — | same as gateway |
| `COOKIE_SECURE` | `true` | set `false` for plain-HTTP local dev |
| `MAX_UPLOAD_MB`… | — | UI uses `max_upload_mb` (200); gateway enforces |

## Layout

- `main.py` — app factory, middleware, error handlers (`main:app`)
- `auth.py` — vendored JWT validation (mirror of gateway)
- `deps.py` — cookie → trusted `Identity`
- `api_client.py` — gateway client bound to the request's token
- `upload_sessions.py` — chunked upload session store
- `routes/` — `auth_pages` (login/logout), `pages` (HTML), `partials` (htmx)
- `templates/` / `static/` — Jinja2 + vendored htmx 2.0.4 + CSS

## Tests

Per-service venv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`,
then `.venv/bin/python -m pytest tests/ -q`.
