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
| `GET` | `/v1/documents` | List tenant documents (`?review_required=`, `?limit=`, `?offset=`) |
| `GET` | `/v1/documents/{id}` | Metadata + versions + artifact references (presigned URLs) |
| `DELETE` | `/v1/documents/{id}` | Authorize, enqueue complete deletion job (202) |
| `GET` | `/v1/documents/{id}/status` | Ingestion job status for the active version |
| `GET` | `/v1/review/items` | List quarantine items (`?status=`) |
| `GET` | `/v1/review/items/{item_id}` | Full quarantine record: render, parse attempts, diagnostics |
| `POST` | `/v1/review/items/{item_id}/resolve` | accept / re-route / reject (**reviewer** role required) |
| `GET` | `/healthz` | Liveness |

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
- `routes/documents.py`, `routes/review.py` — endpoint handlers
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
.venv/bin/python -m pytest tests/ -q   # 34 passed
```
