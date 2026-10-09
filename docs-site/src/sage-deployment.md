# New_sage Deployment

## What it takes

- One machine. CPU-only, no GPU anywhere — EmbeddingGemma 2 and MiniLM are
  CPU-served, and the PDF fallback is pdftext (olmOCR is not used).
- Docker + Docker Compose.
- The two model artifacts staged on the host before first boot
  (EmbeddingGemma 2, MiniLM + tokenizer).
- **No authentication.** All services speak plain HTTP on the `sage-net`
  network. The host firewall / Tailscale interface is the security boundary —
  do not publish these ports to the public internet.

The target is James's H200 box, running alongside new_DRUID.

## Stack layout

Defined in `deploy/docker-compose.yml`. Twelve compose services: the 9
runtime services plus 2 one-shot jobs and 1 internal sidecar.

| Service | Container port | Host port | Notes |
|---|---|---|---|
| `mcp-server` | 8000 | `MCP_HOST_PORT` → 8010 | FastMCP, binds `0.0.0.0:8000` in-container; published for the AI CLI |
| `retrieval` | 8001 | — (internal-only) | |
| `embedding` | 8002 | — (internal-only) | |
| `ingestion-worker` | — | — | RQ `sage-ingest`; scale with `--scale ingestion-worker=4` |
| `crawler-worker` | — | — | RQ `sage-crawl` |
| `milvus-standalone` | 19530 / 9091 | — (internal-only) | `milvusdb/milvus:latest` (CPU); shares RustFS bucket `sage-milvus` |
| `rustfs` | 9000 / 9001 | — (internal-only) | `rustfs/rustfs:1.0.1` (S3-compatible; replaced MinIO, whose community edition was archived in 2026) |
| `postgres` | 5432 | — (internal-only) | `postgres:16.6-alpine` |
| `redis` | 6379 | — (internal-only) | `redis:7.4.2-alpine`, AOF enabled |
| `etcd` | — | — | Internal only; required by Milvus 2.6.x standalone. Not part of the client port contract. |
| `catalog-migrate` | — | — | One-shot: Alembic migrations, then exits |
| `milvus_setup` | — | — | One-shot: provisions `sage_chunks` idempotently |

Host-port policy (2026-10-09): only `mcp-server` publishes a host port;
everything else is container-to-container on the compose/`cora` networks.

Persistent data lives in host bind mounts under `/DATA/docker/mounts/<service>`
(postgres, redis, rustfs, etcd, milvus, model-artifacts) — no named volumes.
Pre-create the directories and fix ownership for non-root images (postgres
UID 70, redis UID 999, rustfs UID 10001); see the deploy README.

The port contract is fixed on the container side. Host-port policy
(2026-10-09): only `mcp-server` publishes a host port (`MCP_HOST_PORT` →
8010, for the AI CLI); everything else is container-to-container.
Inter-service URLs use compose DNS names and fixed container ports, so they
never need changing.

## Model artifact staging

Services are offline-first: they load pre-staged artifacts from the host bind
mount `/DATA/docker/mounts/model-artifacts` (read-only at `/artifacts` inside
containers) and fail startup rather than downloading at runtime
(`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` are set in the stack).
**The build does not include the weights** — stage them once before first
boot by copying the checkpoints straight into the host directory:

```bash
sudo mkdir -p /DATA/docker/mounts/model-artifacts/{embeddinggemma-2,minilm}
sudo cp -r /path/to/your/models/embeddinggemma-2/* /DATA/docker/mounts/model-artifacts/embeddinggemma-2/
sudo cp -r /path/to/your/models/minilm/*           /DATA/docker/mounts/model-artifacts/minilm/
```

Expected host layout:

```
/artifacts/
  embeddinggemma-2/   # google/embeddinggemma-2 checkpoint, offline loadable
  minilm/             # MiniLM reranker checkpoint + tokenizer
```

Each artifact ships with a manifest (pinned revision, sha256, license) in
`services/artifacts/manifests/` (`embeddinggemma-2.yaml`, `minilm-l6-v2.yaml`);
the validator CLI can verify staged artifacts against the manifests. See
[shared-artifact-policy.html](shared-artifact-policy.html) for the policy.

## Bring-up

From the build root (`~/workspace/new_sage/build/deploy`):

```bash
# 0. secrets + any host-port remapping (colocation with new_DRUID)
cp .env.example .env && $EDITOR .env

# 1. base infrastructure
docker compose up -d postgres redis rustfs etcd milvus-standalone

# 2. create the sage_chunks collection + indexes (idempotent; safe to re-run)
docker compose run --rm milvus_setup

# 3. everything else
docker compose up -d

# 4. seed import — enqueue DevDocs/MDN/tldr import jobs on the redis queue
#    consumed by ingestion-worker, writing to Milvus + catalog.
```

Verify:

```bash
docker compose run --rm milvus_setup python -m milvus_setup.create_collection --check-only
curl http://localhost:8010/health   # mcp-server
```

Useful:

```bash
docker compose ps
docker compose logs -f mcp-server retrieval embedding ingestion-worker
docker compose up -d --scale ingestion-worker=4   # more CPU crawl workers
```

Data-loss command (drops `sage_chunks`):

```bash
docker compose run --rm milvus_setup python -m milvus_setup.create_collection --recreate --yes
```

## Refresh jobs

Two automated jobs are specified (§17.6 decision): a **weekly** stale-version
sweep (`refresh_stale`) and a **monthly** DevDocs image sync (`devdocs_sync`).
The weekly sweep is driven by the **`scheduler` compose service**
(`services/scheduler/`): it calls `refresh_stale()` in-process once per
`REFRESH_INTERVAL_SECONDS` (default 7 days), with startup jitter and
failure backoff. The monthly sync is driven by a second compose service,
**`devdocs-scheduler`** (same image, `SCHEDULER_JOB=devdocs_sync`, 30-day
cadence), which enqueues one `devdocs_sync` RQ job per month for the
crawler worker. Details and current status:
[sage-ingestion-freshness.html](sage-ingestion-freshness.html).

## MCP client wiring

Point any MCP client at the Streamable HTTP endpoint:

```
http://<host>:8010/mcp
```

There is no auth header, no token, no tenant concept. Restrict network access
to localhost / the Tailscale interface instead.

## Current limitations (as of the 2026-10-09 build)

- Model weights are **not staged** — required before anything can serve.
- Seed corpus ingestion has **not run** — requires the deployment target.
- Docker image builds were audited statically only; the build VM has no
  Docker. Alembic migrations were exercised on SQLite, not real Postgres.
- `pymilvus` is pinned inconsistently across images (2.4.6 in
  ingestion/crawler vs 2.5.6 in retrieval/milvus_setup) — flagged for
  unification at deploy time.
