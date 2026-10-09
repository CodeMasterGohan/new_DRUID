# new_DRUID — MCP Tools Reference

The MCP server exposes exactly **8 tools** via FastMCP, served over
Streamable HTTP mounted at `/mcp` (port 8002). Every request requires
`Authorization: Bearer <JWT>`; identity comes from the validated token
only. Tool names and parameters below are copied from
`services/mcp_server/src/mcp_server/` (`tools.py`, `server.py`,
`models.py`, `auth.py`) — use them verbatim.

## Common rules (all tools)

- Tenant identity is read only from the validated request context; tool
  arguments never carry tenant/user/group IDs.
- All string IDs are opaque (`^[A-Za-z0-9_-]{1,128}$`); paths, `../`, and
  absolute paths are rejected.
- Every response instructs the calling agent to treat retrieved `text`
  fields as **untrusted data, never instructions**.
- Role enforcement comes from `auth.TOOL_ROLES`. A caller needs one of
  the listed roles (admin always passes).

## search_knowledge_base

Authorized hybrid retrieval over the tenant's knowledge base. Runs dense +
BM25 + identifier retrieval with the caller's tenant predicate applied to
every branch, fuses candidates (RRF, k=60), reranks with MiniLM, and
returns provenance-tagged passages. Inactive, deleting, and unreviewed
versions are excluded.

| Parameter | Type | Constraints | Notes |
|---|---|---|---|
| `query` | string | 1–2000 chars, non-blank | Natural-language question or keyword query |
| `top_k` | int | 1–20, default 8 | Max passages to return |

Returns `{"passages": [{provenance, text}, ...]}`. Provenance carries
`document_id`, `filename` (required), page range, and the
`untrusted-document-content` trust marker.

**Roles:** `member`, `reviewer`, `admin`.

## ingest_document

Registers an authorized upload and enqueues ingestion. Validates the file
declaration, checks the tenant's ingestion quota, creates the catalog
record and durable job, and returns a **single-use upload ticket**. The
actual bytes are uploaded through the Document API with that ticket — this
tool never fetches remote URLs.

| Parameter | Type | Constraints |
|---|---|---|
| `filename` | string | Bare display name, never a path; allowlisted extensions |
| `mime_type` | string | One of `application/pdf`, `image/png`, `image/jpeg`, `image/tiff` |
| `file_size_bytes` | int | 1 … 200 MiB (matches `MAX_UPLOAD_MB`) |
| `content_sha256` | string | Hex SHA-256 of the file bytes |

Returns `{document_id, job_id, upload_ticket,
upload_ticket_expires_in_seconds, note}`. Follow up by uploading the bytes
to the Document API, then poll `get_ingestion_status`.

**Roles:** `member`, `reviewer`, `admin`.

## get_ingestion_status

Returns the status of an ingestion job owned by the caller's tenant.

| Parameter | Type | Notes |
|---|---|---|
| `job_id` | string | Opaque job ID from `ingest_document` |

Returns `{job_id, document_id, state, progress_percent, detail}`. `state`
is one of the `IngestionState` values (`UPLOADED → QUEUED → PARSING →
CHUNKING → EMBEDDING → INDEXING → READY`, plus `RETRY_PENDING`, `FAILED`,
`REVIEW_REQUIRED`, `DELETING`, `DELETED`).

**Roles:** `member`, `reviewer`, `admin`.

## list_documents

Lists documents owned by the caller's tenant. Supports the
`review_required` filter for the quarantine surface. Only bounded,
enumerated filters are accepted — there is no free-form query interface.

| Parameter | Type | Constraints |
|---|---|---|
| `review_required` | bool \| null | `true`: only documents awaiting reviewer attention |
| `status` | string \| null | One of the `IngestionState` enum values listed above |
| `limit` | int | 1–100, default 50 |
| `offset` | int | ≥ 0, default 0 |

Returns `{total, documents: [{document_id, filename, status, version,
page_count, created_at}]}`.

**Roles:** `member`, `reviewer`, `admin`.

## get_document

Returns metadata (and optionally opaque artifact references) for one
document owned by the caller's tenant. Artifact references are opaque
keys, never filesystem paths. Unknown or foreign IDs fail.

| Parameter | Type | Constraints |
|---|---|---|
| `document_id` | string | Opaque document ID |
| `include_artifacts` | bool | default `false` — include opaque artifact references |

Returns `{document_id, filename, status, version, page_count, created_at,
artifact_refs}`.

**Roles:** `member`, `reviewer`, `admin`.

## get_review_item

Returns one quarantine item: the page render reference, both parse
attempts, and routing diagnostics. Use this to inspect pages the router
could not confidently parse before deciding accept / re-route / reject.
Quarantined content is never searchable.

| Parameter | Type | Notes |
|---|---|---|
| `review_item_id` | string | Opaque review item ID |

Returns `{review_item_id, document_id, page_index, page_render_ref,
native_attempt_summary, visual_attempt_summary, routing_diagnostics}`.

**Roles:** `reviewer`, `admin`.

## resolve_review_item

Applies **accept / re-route / reject** to a quarantined page. Requires the
reviewer role **and** explicit client confirmation (`confirm: true`).
Without `confirm=true` the tool returns `{applied: false, reason: ...}`
and re-invocation is required.

| Parameter | Type | Constraints |
|---|---|---|
| `review_item_id` | string | Opaque review item ID |
| `decision` | string | One of `accept`, `reroute`, `reject` |
| `note` | string \| null | ≤ 500 chars |
| `confirm` | bool | default `false` — must be `true` to apply |

Returns `{applied: true, new_state}` on success.

**Roles:** `reviewer`, `admin`.

## delete_document

Authorizes and enqueues complete deletion of a document owned by the
caller's tenant. Requires explicit client confirmation (`confirm: true`);
without it the tool returns `{applied: false, reason: ...}`. Deletion
covers Milvus chunks, originals, derived artifacts, page renders, caches,
and catalog visibility. The job is not `DELETED` until every store
confirms.

| Parameter | Type | Notes |
|---|---|---|
| `document_id` | string | Opaque document ID |
| `confirm` | bool | default `false` — must be `true` to enqueue deletion |

Returns `{applied: true, deletion_state}` on success.

**Roles:** `member`, `reviewer`, `admin`.

## Role summary

| Tool | member | reviewer | admin |
|---|---|---|---|
| search_knowledge_base | ✓ | ✓ | ✓ |
| ingest_document | ✓ | ✓ | ✓ |
| get_ingestion_status | ✓ | ✓ | ✓ |
| list_documents | ✓ | ✓ | ✓ |
| get_document | ✓ | ✓ | ✓ |
| get_review_item | | ✓ | ✓ |
| resolve_review_item | | ✓ | ✓ |
| delete_document | ✓ | ✓ | ✓ |

## Transport notes

- Streamable HTTP: `POST` to the `/mcp` mount with the Bearer `<redacted>`
  A separate **stdio adapter** exists with an explicit trusted credential
  context (`--user-id`, `--group-id`, `--roles`); gateway headers never
  apply to it.
- Origin allowlist (`MCP_ALLOWED_ORIGINS`): requests with a non-allowlisted
  `Origin` header are rejected with 403 (DNS-rebinding protection);
  requests without `Origin` (native clients) are allowed.
- Compose note: the shipped compose entry runs the server with
  `DRUID_MCP_DEV_FAKE_CLIENTS=1` (in-memory fakes) until the real
  retrieval/ingestion/catalog HTTP clients land — dev/fake clients only,
  never real tenant data (see [Troubleshooting](druid-troubleshooting.html)).
