# Gateway (`services/gateway`)

Authenticated reverse-proxy gateway for new_DRUID (spec §4). Every request
is authenticated, quota-checked, and forwarded to an internal service with
HMAC-signed identity headers.

## Request lifecycle

1. **Strip** spoofable identity headers (`druid_internal.SPOOFABLE_HEADERS`)
   via middleware — clients can never smuggle `X-Group-Id`, `X-User-Id`,
   or `X-Druid-*` past this point.
2. **Authenticate** — `Authorization: Bearer <JWT>` is validated for
   signature, issuer, audience, and expiry (`auth.validate_token`). The
   trusted `RequestContext` is built from token claims **only**.
3. **Quota** — per-tenant ingestion pages/day and query QPS (`quotas`).
   Exhaustion → `429` with `Retry-After`. Uploads are also capped at
   `quota_max_upload_mb` (`413`) and `quota_max_pages_per_document` (`400`).
4. **Forward** — `proxy.forward_request` sends the request to the internal
   service with the signed `X-Druid-*` headers over the internal connection.

## Layout

| File | Purpose |
|---|---|
| `app.py` | FastAPI app factory `create_app(settings)`; routes `/healthz`, `POST /v1/documents/upload`, `POST /mcp`, catch-all `/v1/{path}` → document API |
| `auth.py` | JWT validation (HS256 shared secret or RS256 via JWKS). `group_id` claim required; missing/invalid → `AuthError` (fail closed) |
| `druid_internal.py` | HMAC-SHA256 signing/verification of the forwarded identity (`X-Druid-*` headers); `SPOOFABLE_HEADERS` set; header contract documented for sibling services |
| `quotas.py` | `QuotaConfig`, `QuotaDecision`, `QuotaStore` interface, `InMemoryQuotaStore` (tests/dev), `RedisQuotaStore` (production, atomic Lua) |
| `proxy.py` | Upstream forwarding (drops hop-by-hop + spoofable headers, adds signed identity headers) |

## Quota classification

* `POST /v1/documents/upload` — ingestion quota, `page_count` from query/form.
* `POST /mcp` with `tools/call` → `ingest_document` — ingestion quota,
  `page_count` from tool arguments. All other MCP calls — query QPS quota.
  Tool arguments influence **only** the quota class, never identity.
* Other `/v1/*` routes — query QPS quota.

## Configuration (env — names follow the shared `.env.example`, no prefix)

| Variable | Default | Purpose |
|---|---|---|
| `JWT_SECRET` | — | HS256 secret (required for HS256) |
| `JWT_ALGORITHMS` | `HS256` | Allowlisted algorithms |
| `JWT_ISSUER` / `JWT_AUDIENCE` | — | Expected `iss` / `aud` |
| `JWKS_URL` | — | JWKS URL for RS256/ES256 |
| `INTERNAL_SECRET` | `change-me-in-production` | HMAC secret for `X-Druid-*` headers (add to shared `.env.example`) |
| `REDIS_URL` | — | If set, quotas use Redis; otherwise in-memory |
| `DOCUMENT_API_URL` / `MCP_SERVER_URL` | `http://document-api:8001` / `http://mcp-server:8002` | Upstreams (add to shared `.env.example`) |
| `QUOTA_INGEST_PAGES_PER_DAY` | `1000` | §4 default |
| `QUOTA_QUERY_QPS` | `10` | §4 default |
| `MAX_UPLOAD_MB` | `200` | §4 default |
| `MAX_PAGES_PER_DOCUMENT` | `2000` | §4 default |

## Run

```bash
cd services/gateway
uvicorn services.gateway.app:app --host 0.0.0.0 --port 8000
# (from the build root so `services.*` imports resolve)
```

## Tests

```bash
cd /home/hatch/workspace/new_druid/build
python -m pytest services/gateway/tests -q
```
