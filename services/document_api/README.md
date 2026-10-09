# document_api — Document + Review-Queue API

FastAPI service implementing the new_DRUID document API surface (spec §11).
Runs **behind the gateway**: the gateway authenticates the bearer token,
enforces quotas, strips spoofable headers, and forwards with HMAC-signed
`X-Druid-*` identity headers. This service verifies that signature on every
request and fails closed without it (§4).

## Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/documents/upload` | Multipart upload → validate → store → catalog → enqueue ingestion. Returns `{document_id, job_id, status}` |
| `POST` | `/v1/documents/register` | Two-phase upload, step 1: validate the file declaration → reserve the catalog record + ingestion job → issue a single-use upload ticket. Returns `{document_id, job_id, upload_ticket, expires_in_seconds}` (201) |
| `POST` | `/v1/documents/{id}/bytes` | Two-phase upload, step 2: redeem the ticket with the multipart bytes → verify against the declaration → store → enqueue ingestion. Returns `{document_id, job_id, status}` |
| `GET` | `/v1/jobs/{job_id}` | Tenant-scoped job status: state, type, attempts, error excerpt, timestamps. Cross-tenant reads as 404 |
| `GET` | `/v1/documents` | List tenant documents (`?review_required=`, `?limit=`, `?offset=`) |
| `GET` | `/v1/documents/{id}` | Metadata + versions + artifact references (presigned URLs) |
| `DELETE` | `/v1/documents/{id}` | Authorize, enqueue complete deletion job (202) |
| `GET` | `/v1/documents/{id}/status` | Ingestion job status for the active version |
| `GET` | `/v1/review/items` | List quarantine items (`?status=`) |
| `GET` | `/v1/review/items/{item_id}` | Full quarantine record: render, parse attempts, diagnostics |
| `POST` | `/v1/review/items/{item_id}/resolve` | accept / re-route / reject (**reviewer** role required) |
| `GET` | `/healthz` | Liveness |

## Two-phase upload: ticket registration + redemption (§5)

The MCP server's `ingest_document` tool deliberately never holds file
bytes, so a caller that only has a file *declaration* (name, MIME, size,
SHA-256) cannot use the single-shot `POST /v1/documents/upload`. The
two-phase flow closes that gap:

1. `POST /v1/documents/register` takes the JSON declaration, validates it
   (display-name sanitizing, MIME allowlist, size cap, SHA-256 shape),
   creates the catalog document record and the durable ingestion job in
   state `UPLOADED` (**not** enqueued — no bytes exist yet), and returns a
   single-use upload ticket (`upload_ticket_ttl_seconds`, default 900s).
2. The caller uploads the bytes with `POST /v1/documents/{id}/bytes`
   (multipart `file` + form field `upload_ticket`). The ticket is
   constant-time-compared, TTL-bound, and pinned to the tenant, document,
   and declaration; the bytes must match the declared size and SHA-256
   exactly, and the sniffed MIME type must agree with the declared one.
   On success the version record is created, bytes are stored under the
   tenant-scoped key, the ticket is marked consumed, and the registration
   job is promoted to a real ingestion job and enqueued — from there the
   flow is identical to the single-shot upload (commit before enqueue;
   enqueue failure parks the job as `FAILED`).

Design decision (2026-10-09): the ticket is a *real* authorization token
for the byte-upload step, not just an idempotency-slot reservation. An
idempotency slot alone would have nothing to redeem it — the single-shot
upload route creates its own document/version/job and takes no ticket —
whereas the register-then-redeem split is exactly what the MCP
`IngestionServiceClient` ABC (`register_upload` → `UploadTicket`,
`get_job_status` by `job_id`) was written against. Ticket state lives on
the job row's payload (no new table / migration); wrong ticket → 403,
consumed → 409, expired → 410. Quota enforcement stays at the gateway
(§4), mirroring the single-shot upload route, which performs no quota
accounting itself.

## Upload validation (§5)

- Magic-byte MIME sniffing (PDF/PNG/JPEG/WebP/TIFF); declared
  `Content-Type` must agree or be `application/octet-stream`.
- Size cap `MAX_UPLOAD_MB` (413 when exceeded), streamed in 1 MiB chunks.
- Page count from the PDF trailer via `pypdf` (never a full parse);
  `MAX_PAGES_PER_DOCUMENT` enforced (400 when exceeded).
- SHA-256 checksum; opaque UUID `document_id`; filename is display
  metadata only and never enters storage keys.
- Originals stored under tenant-scoped keys
  `{group_id}/{document_id}/{version_id}/original`.
- DB commit happens **before** the RQ enqueue so a worker can never race
  the commit and miss its job row. If the enqueue fails, the job is parked
  as `FAILED` instead of hanging in `UPLOADED`.

## Identity (§4)

`internal_auth.py` is a byte-exact mirror of the gateway's
`druid_internal.py` signing contract (`druid-v1|…` canonical string,
HMAC-SHA256, `v1:<hex>`). Kept local so this image stays independent of
the gateway package. `tests/test_entrypoints.py`-style cross-compat is
covered in `test_document_api.py` via the gateway's own
`build_internal_headers`.

## Configuration

See `settings.py`; names follow the shared `.env.example` (no `DRUID_`
prefix). `INTERNAL_SECRET` must be set — an empty secret fails every
request closed with 401.

## Layout

- `main.py` — `create_app()` factory + `app` (uvicorn `main:app`)
- `routes/documents.py`, `routes/review.py`, `routes/jobs.py` — endpoint handlers
- `internal_auth.py` — HMAC verification / trusted context
- `deps.py` — settings, sessions, repository, object store wiring
- `storage.py` — `ObjectStore` ABC, `MemoryObjectStore`, `RustFSObjectStore`
- `job_queue.py` — durable job creation + RQ enqueue by dotted-path worker
  entry points (`services.ingestion.pipeline.run_ingestion_job` /
  `run_deletion_job`); the strings are asserted in `test_entrypoints.py`
- `validation.py` — MIME sniffing, page counting
- `schemas.py` — pydantic request/response models

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -q   # 71 passed
```
