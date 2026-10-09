# mcp_server — new_DRUID MCP server

FastMCP server exposing the 8 tools from spec §11 over two transports:

| Transport | Auth | Use |
|---|---|---|
| Streamable HTTP (`/mcp`, port 8002) | Bearer JWT (signature, issuer, audience, expiry) | Network clients, behind the API gateway |
| stdio (`python -m mcp_server.stdio_adapter`) | Explicit `--user-id`/`--group-id`/`--roles` (or `DRUID_STDIO_*` env) | Local trusted use only |

The two transports are **separate processes**. Gateway headers never apply to
stdio. MCP session IDs are never used as an authentication identity.

## Tools

`search_knowledge_base`, `ingest_document`, `get_ingestion_status`,
`list_documents` (with `review_required` filter), `get_document`,
`get_review_item`, `resolve_review_item` (reviewer role + `confirm: true`),
`delete_document` (`confirm: true`).

Argument rules (§11): no tool accepts tenant/user/group IDs, arbitrary DB
filters, filesystem paths, or remote-fetch URLs. Tenant identity comes only
from the validated request context (§4) — missing context fails closed.

Responses (§10): every passage is wrapped in `<<<druid:passage>>>`
delimiters carrying `trust="untrusted-document-content"`, the modality
(`text` | `page_image`), and `page_image_ref` for image records. Tool
descriptions instruct callers to treat `text` fields as untrusted data,
never instructions.

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `JWT_SECRET` or `JWT_PUBLIC_KEY_PEM` | yes (one) | Bearer token verification (HS256 / RS256) |
| `JWT_ISSUER` | yes | Expected `iss` claim |
| `JWT_AUDIENCE` | yes | Expected `aud` claim |
| `MCP_ALLOWED_ORIGINS` | no | Comma-separated allowlist for the `Origin` header (DNS-rebinding protection). Requests *without* Origin are allowed; requests *with* a non-allowlisted Origin get 403. |
| `RETRIEVAL_URL` | yes | Base URL of the retrieval service (`POST /search`), e.g. `http://retrieval:8004` |
| `DOCUMENT_API_URL` | yes | Base URL of the document API (documents/review/delete), e.g. `http://document_api:8001` |
| `INTERNAL_SECRET` | yes | HMAC secret for signing downstream `X-Druid-*` identity headers (shared with gateway/backends) |
| `MCP_DOWNSTREAM_TIMEOUT_SECONDS` | no | Timeout (s) for downstream HTTP calls (default 30) |
| `DRUID_MCP_DEV_FAKE_CLIENTS=1` | dev only | Use in-memory fake service clients instead of the HTTP clients above |

Token claims: `sub` (user), `grp` (tenant), `roles` (list). The request ID
is generated server-side per request.

## Running

```bash
# dev (fake downstream services)
DRUID_MCP_DEV_FAKE_CLIENTS=1 JWT_SECRET=dev-secret \
  uvicorn --factory mcp_server.server:create_app --port 8002

# stdio (local trusted user)
python -m mcp_server.stdio_adapter --user-id alice --group-id acme \
  --roles member,reviewer --dev-fake-clients
```

Production wiring: `create_app()` builds real httpx-based
`RetrievalServiceClient` / `IngestionServiceClient` / `CatalogClient`
implementations (`http_clients.py`) from `RETRIEVAL_URL` /
`DOCUMENT_API_URL` / `INTERNAL_SECRET` by default; a caller-supplied `deps`
overrides. Without configured URLs/secret the factory raises — it never
silently runs unauthenticated or unwired. Catalog reads go through
document_api's existing endpoints (there is no direct catalog HTTP API);
`register_upload` and job-scoped `get_job_status` have no backing endpoints
and raise `NotImplementedError` — see `http_clients.py` for the documented
gaps.

## Layout

```
src/mcp_server/
  context.py       Trusted RequestContext (contextvar, fail-closed)
  auth.py          TokenVerifier ABC, JWTVerifier, TOOL_ROLES
  models.py        Pydantic arg validation + §10 provenance result contract
  clients.py       Service client ABCs + in-memory fakes (dev/test only)
  tools.py         The 8 tool implementations
  server.py        FastMCP app, Streamable HTTP, AuthMiddleware, Origin check
  stdio_adapter.py Separate stdio entrypoint, explicit credential context
tests/             Contract tests (tool schemas, arg validation, auth,
                   provenance, middleware, stdio)
```

## Tests

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
```

## Notes / gaps

- Downstream HTTP clients (retrieval/ingestion/catalog services) are not
  implemented here — sibling agents own those services; this server calls
  them only through the ABCs.
- `delete_document`/`resolve_review_item` require `confirm: true`
  (client-side confirmation support per §11).
- Quota enforcement lives at the gateway (§4); the MCP server re-validates
  auth but does not duplicate quota accounting.
