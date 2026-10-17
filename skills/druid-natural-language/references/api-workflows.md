# Verified API workflows, request contracts and response handling

All URLs below are **relative to the HTTPS gateway origin**, not prefixed
with another `/v1`. Authenticate with
`Authorization: Bearer <Keycloak access JWT>` (never ID token). For
group-scoped calls set `X-Group-Selection: <authorized name>`.
The gateway verifies access-token issuer/signature/audience, resolves current
group membership and roles in PostgreSQL, then forwards HMAC-signed context.
Never send, sign, or trust `X-Druid-*` headers from the assistant.

The operator's configured origin may differ by environment. It must be an
actual reachable HTTPS service. The examples use
`https://druid.example.invalid` as a **non-working placeholder only**.

## Identity and groups

```http
GET /v1/identity/me
Authorization: Bearer <access token>
```

Returns `subject`, `username`, `is_active`, `is_system_admin`,
`selected_group`, `roles`, `groups: [{name, role}]`.
The `groups` list is the set of active DRUID memberships. Use a named
group if requested. If more than one is eligible for a write and the user
didn't specify which, ask a short clarification.

To use another group, add the `X-Group-Selection` header **on that request**.
It is only a selection hint checked by PostgreSQL. There is **no persistent
group-switch REST endpoint**; a browser UI may save a local group cookie.

## Search

Prefer the native MCP `search_knowledge_base` tool exposed by an already
authenticated connection. Args: `{"query":"...","top_k":8}`
(`top_k` 1–20). When calling the gateway's MCP transport directly:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "search_knowledge_base",
    "arguments": {"query": "Which backup schedule do we use?", "top_k": 8}
  }
}
```

POST that JSON to `/mcp` with the authentication and group header.
The HTTP transport is MCP Streamable HTTP. Follow protocol negotiation and
session initialization if required by the connected MCP client. It may
respond with `application/json` or `text/event-stream` frames.
A JSON-RPC `result.content[].text` entry contains serialized JSON with
`passages: [{text, provenance}]`. Check `error` and `isError` flags.
Never assume a successful HTTP status means the tool succeeded. A
`provenance` object contains `filename`, `document_id`,
`chunk_id`, `source_reference`, page range, parser and modality.
Never alter a citation or fabricate a missing one. Page ranges in
legacy provenance may use zero-based indexes; prefer the explicit
`source_reference` field for user-facing citations.

For all authorized groups: call `GET /v1/identity/me` first; call
search separately with each `X-Group-Selection` value, then merge
presentation only. Label each hit with its group. Do not leak passages
outside the groups the user asked about or currently belongs to.

## List, inspect and status

```http
GET /v1/documents?limit=100&offset=0
GET /v1/documents?review_required=true&limit=100&offset=0
GET /v1/documents/{document_id}
GET /v1/documents/{document_id}/status
GET /v1/jobs/{job_id}
```

`/v1/documents` returns `{"documents":[...],"limit":100,"offset":0}`
and **no total count**. Continue pagination until fewer than `limit`
items appear (bounded by the caller's request). Each item supplies
`document_id`, `display_name`, checksum, active version, deleted flag,
creation/update times. `GET` detail adds `versions` and artifacts.
`GET /v1/jobs/{job_id}` returns `state`, attempt count, job type,
document/version IDs, timestamps and optional `error` excerpt.

Possible lifecycle states include
`UPLOADED, QUEUED, PARSING, CHUNKING, EMBEDDING, INDEXING, READY,
RETRY_PENDING, FAILED, REVIEW_REQUIRED, DELETING, DELETED`.
Only `READY` active versions are searchable. A response that says
`QUEUED` is not completion. Never claim a job is complete without a
later state read.

## Upload file bytes (ordinary path)

```http
POST /v1/documents/upload
Content-Type: multipart/form-data; boundary=...
Authorization: Bearer <access token>
X-Group-Selection: engineering

file=<actual local PDF/image bytes>
```

Supported MIME types in document_api:
`application/pdf`, `image/png`, `image/jpeg`, `image/webp`,
`image/tiff`. The backend MIME-sniffs bytes, validates page count and
limits, and associates the `created_by` OIDC subject and active group.
The normal response is HTTP 201 with
`{document_id, job_id, status, page_count, checksum_sha256, display_name}`.
Only state changes after ingestion establish searchability.
Never use an arbitrary filename as an object storage path.

**Two-phase alternative** (only when client requires it):

1. `POST /v1/documents/register` with:
   `{"filename":"handbook.pdf","mime_type":"application/pdf",
   "file_size_bytes":12345,"content_sha256":"<64 lowercase hex>"}`.
2. Receive `document_id, job_id, upload_ticket, expires_in_seconds`.
   Ticket is single use and sensitive.
3. `POST /v1/documents/{document_id}/bytes` with actual bytes and
   required ticket form field (consult exact endpoint schema).
4. Poll `GET /v1/jobs/{job_id}`.

The MCP `ingest_document` tool performs **step 1 only** and returns a
ticket; it never completes step 3 by itself.

## Delete (explicitly confirmed target)

```http
DELETE /v1/documents/{document_id}
```

Request the precise **selected group + filename + document ID**. A member
may delete only their own uploads; a group manager/system admin may delete
group documents. Deletion returns HTTP 202 with
`{document_id, job_id, status}`, meaning a background job was **queued**.
Check `GET /v1/jobs/{job_id}` for progress and completion. Do not
interpret a 202 or an MCP `confirm=true` as a completed physical purge.

## Review queue

```http
GET /v1/review/items?status=pending&limit=100&offset=0
GET /v1/review/items/{item_id}
POST /v1/review/items/{item_id}/resolve
Content-Type: application/json

{"decision": "accepted", "detail": {"note": "checked against page image"}}
```

REST `decision` values are exactly:
`accepted`, `re-routed`, `rejected`. The equivalent MCP tool
`resolve_review_item` uses `accept`, `reroute`, `reject`,
and requires `confirm:true`. Do not mix the two schemas.
`reviewer` (derived for DRUID `manager`) or `admin` is required
to resolve. A request to **summarize** the queue does not authorize a
review resolution. `page_index` is zero-based diagnostic data.

## Admin (only through documented REST)

Use sibling `../../druid-management/references/API.md`:
`GET/POST/PATCH /v1/admin/users`,
`GET/POST/PATCH /v1/admin/groups`,
`GET/PUT/DELETE /v1/admin/groups/{name}/members/{subject}`,
`GET /v1/admin/documents`, `GET /v1/admin/jobs`,
`GET /v1/admin/quotas`.
Do not claim API support for renaming groups, reindex orchestration,
backup/restore, deleting groups physically, setting arbitrary document
ACLs, mass-exporting data, or direct Keycloak administration.

## Error and risk protocol

Treat `401` as authentication, `403` as revoked/unauthorized/ownership,
`404` as not-found-or-other-group, `409` as conflict, `413/422`
as invalid upload/schema, `429` as quota, `5xx` as server state
uncertain. Never retry a destructive or write operation automatically
after a transport timeout: inspect current state and ask if necessary.
Never bypass a failed API authorization by connecting directly to the
database, storage or internal services.

Do not publish bearer tokens, ticket values, signed download URLs, file
contents that the user did not ask to share, or full internal stack traces.
