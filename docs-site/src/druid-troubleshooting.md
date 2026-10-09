# new_DRUID — Troubleshooting

Every entry below has a source basis: the build log's wiring-fix and gap
sections, the deploy files, or the service code itself.

## Startup failures

### Model artifact missing → service fails at startup

The embedding service, retrieval service, ingestion workers, and vLLM
mount `${MODEL_DIR:-/models}` **read-only** and run with
`HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`. A missing or invalid
checkpoint (olmOCR-2-7B-1025-FP8, embeddinggemma-2, or
cross-encoder/ms-marco-MiniLM-L6-v2) **fails startup loudly and names the
artifact and the expected mount path** — it never downloads at runtime.
This is the intended offline-first behavior, not a bug. Fix: complete the
artifact manifests (`services/artifacts/manifests/`) and stage the
checkpoint under `$MODEL_DIR/<org>/<name>` before starting.

### mcp_server crash-loops in compose

`create_app()` raises `RuntimeError` unless real service clients are
wired or `DRUID_MCP_DEV_FAKE_CLIENTS=1` is set. The shipped compose file
sets `DRUID_MCP_DEV_FAKE_CLIENTS: "1"` (with a TODO to remove once the
real retrieval/ingestion/catalog HTTP clients land). If you removed it
without wiring real clients, the container restarts forever — re-add the
flag or wire the clients. Note: dev/fake clients must never serve real
tenant data.

### Gateway Dockerfile CMD

An earlier wiring bug had the gateway CMD pointing at a `main:app`
entrypoint that didn't exist (the gateway package ships `app.py`). This
was fixed to `uvicorn app:app` and is covered by a deploy-wiring test —
if you hit an import error on gateway startup, check the CMD against the
module layout first.

## Port and network collisions

### Host port collision: gateway vs vLLM

The original `.env.example` set `GATEWAY_PORT=8000` and `VLLM_PORT=8000`
on the same H200 host — both compose projects bind the host port. The
fix: `GATEWAY_PORT=8080` (container still listens on 8000; only the host
mapping moved). A deploy-wiring test asserts no host-port collision
across the two compose projects. If services fail to bind, check this
first.

### Ingestion workers can't reach vLLM

Two related fixes landed: the olmOCR client defaulted to
`http://olmocr-vllm:8000` (no such service) — corrected to
`http://vllm-olmocr:8000` (or set `OLMOCR_VLLM_URL`) — and the CPU and
GPU compose projects shared no network. Both projects must attach to the
shared **external `druid-net`** (`docker network create druid-net` once
per host); `ingestion` and `vllm-olmocr` attach to it so workers resolve
`vllm-olmocr` by service name. Without it, DNS resolution fails and
visual-path pages error out.

## Database and migrations

### Alembic not run

`docker compose run --rm catalog` (alembic upgrade head) must exit 0
**before** starting services. The migration module imports cleanly but
has not been executed against live PostgreSQL in this environment —
watch the one-shot job's output on first deploy. `alembic/env.py`
requires `DATABASE_URL` and refuses to guess.

### Auth failures across services (401/403 on internal hops)

The gateway signs `X-Druid-*` identity headers with `INTERNAL_SECRET`;
document_api, retrieval, and others verify them byte-exactly and fail
closed. If internal calls 401/403 while direct calls work, the
`INTERNAL_SECRET` values differ between services (or are unconfigured —
"Never commit a filled value", and the template ships it empty). The
mcp_server's `JWT_SECRET` / `JWT_PUBLIC_KEY_PEM` pair is separate and
required for bearer-token verification.

## Quotas and Redis

### Redis down → quota usage reads as null, not zero

The admin quotas page reports `ingest_pages_used` / `query_qps` as
**null** when Redis is unreachable, rather than a misleading zero
(logged at WARNING). If you see nulls, check Redis — don't treat it as
"no usage". Non-admin quota behavior: without Redis, the gateway falls
back to the `InMemoryQuotaStore`, which is correct for a single process
only; in a multi-replica deployment, quota accounting silently drifts.
The Redis Lua quota scripts are not exercised against a live Redis in
this build (named gap — needs an integration test).

### Quota tuning is provisional

`QUOTA_INGEST_PAGES_PER_DAY=1000` and `QUOTA_QUERY_QPS=10` are estimates.
The runbook requires a throughput burn-in before production traffic —
measure pages/hour against the H200 and adjust. Ingestion concurrency
and quotas are the levers for GPU KV-cache pressure (see below).

## vLLM / GPU

### OOM or KV-cache pressure under burst

Do **not** lower `--gpu-memory-utilization` below 0.7 as a first move —
that is the wrong direction. Cap ingestion worker concurrency
(`--scale ingestion=N`) and/or the ingestion quota instead. Measure
olmOCR pages/hour while embedding/query services are under load (§13
GPU-arbitration check).

### vLLM image / FP8 on Hopper

The compose entry pins `vllm/vllm-openai:v0.7.3` with a TODO to verify
the tag at deploy time — FP8 on Hopper (sm_90a) requires a build with
FP8 kernels. Also verify the image ships `curl`: the healthcheck is
`curl -f http://localhost:8000/health` (named gap, flagged 2026-10-08).

## Image and dependency notes

### MinIO tag unpinned

Compose uses `minio/minio:latest` with a TODO to pin a dated RELEASE tag
at deploy time (the tag could not be verified offline). Pin it before
production.

### Dependency pins are initial lockfiles

Every `requirements.txt` has `==` pins, but `pip-compile` was never run
against PyPI for a release (regenerate before release). Two phantom pins
were already caught and fixed: `pdftext==0.4.2` → `0.4.1` and
`olmocr==0.4.0` → `0.4.27` (the 0.4.0 line never published; 0.4.27 keeps
transformers under the `gpu` extra only, and its prompt builder is
byte-identical). EmbeddingGemma 2 required a re-pin to
`sentence-transformers==6.1.0` / `transformers==5.18.0` (the released
checkpoint needs ST ≥ 6.1.0 / transformers 5.x). If model loading fails
with import or API errors, re-check the pins before blaming the
checkpoint.

## Retrieval quality and activation

### Documents never become searchable

Only `READY` **active** versions are searchable (the catalog hard
filter). If a document is stuck: check its status — `REVIEW_REQUIRED`
means pages are parked in the review queue (human decision needed, never
auto-retried); `FAILED` shows diagnostics in the Quarantine UI page;
`activate_version()` additionally refuses when the chunk count falls
outside `[min,max]_chunks_per_page × page_count` from
`parser_revision_stats` — which must be **seeded from the M1 eval corpus**
before range checks are meaningful.

### Router thresholds are provisional

The page router's thresholds (text density, replacement-char ratio, etc.)
are marked PROVISIONAL pending the ≥95% routing-accuracy gate on the M1
eval corpus. Systematic mis-routing (good pages quarantined, or visual
pages parsed natively) is expected until M1 calibration — don't tune by
hand; build the corpus first.

## Checklist summary

1. `docker network create druid-net` before `up`.
2. Secrets filled in `.env` (`INTERNAL_SECRET`, `JWT_SECRET`,
   `POSTGRES_PASSWORD`, `MINIO_ROOT_PASSWORD`); template values are not
   secrets.
3. Model checkpoints staged at `$MODEL_DIR` per the manifests.
4. One-shot `catalog` + `milvus_setup` jobs exit 0 before starting
   services.
5. No host-port collision across compose projects (gateway 8080, vLLM
   8000, document_api 8001, mcp_server 8002, embedding 8003, retrieval
   8004, web_ui 8005).
6. Burn in quotas before production traffic.
