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

| Service | Container port | Default host port | Notes |
|---|---|---|---|
| `mcp-server` | 8000 | `MCP_HOST_PORT` → 8010 | FastMCP, binds `0.0.0.0:8000` in-container |
| `retrieval` | 8001 | `RETRIEVAL_HOST_PORT` → 8011 | |
| `embedding` | 8002 | `EMBEDDING_HOST_PORT` → 8012 | |
| `ingestion-worker` | — | — | RQ `sage-ingest`; scale with `--scale ingestion-worker=4` |
| `crawler-worker` | — | — | RQ `sage-crawl` |
| `milvus-standalone` | 19530 / 9091 | `MILVUS_HOST_PORT` → 19531 / `MILVUS_MGMT_HOST_PORT` → 9092 | `milvusdb/milvus:latest` (CPU); shares RustFS bucket `sage-milvus` |
| `rustfs` | 9000 / 9001 | `RUSTFS_HOST_PORT` → 9010 / `RUSTFS_CONSOLE_HOST_PORT` → 9011 | `rustfs/rustfs:1.0.1` (S3-compatible; replaced MinIO, whose community edition was archived in 2026) |
| `postgres` | 5432 | `POSTGRES_HOST_PORT` → 5433 | `postgres:16.6-alpine` |
| `redis` | 6379 | `REDIS_HOST_PORT` → 6380 | `redis:7.4.2-alpine`, AOF enabled |
| `etcd` | — | — | Internal only; required by Milvus 2.6.x standalone. Not part of the client port contract. |
| `catalog-migrate` | — | — | One-shot: Alembic migrations, then exits |
| `milvus_setup` | — | — | One-shot: provisions `sage_chunks` idempotently |

Named volumes: `pgdata`, `redisdata`, `rustfsdata`, `etcddata`, `milvusdata`,
`model-artifacts`.

The port contract is fixed on the container side. The default host ports sit
in a separate block from new_DRUID's (gateway 8080, vLLM 8000, document_api
8001, mcp 8002, embedding 8003, retrieval 8004, web_ui 8005, rustfs 9000/9001,
milvus 19530/9091), so the two stacks no longer collide out of the box. If
you need different ports, remap the `${*_HOST_PORT}` variables in `.env` —
inter-service URLs use compose DNS names and fixed container ports, so they
never need changing.

## Model artifact staging

Services are offline-first: they load pre-staged artifacts from the
`model-artifacts` volume (read-only) and fail startup rather than downloading
at runtime (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` are set in the
stack). **The build does not include the weights** — stage them once before
first boot:

```bash
# 1. create the volume (actual name: <project>_model-artifacts; check `docker volume ls`)
docker volume create new_sage_model-artifacts

# 2. copy the models in (volume mount path is /artifacts inside containers)
docker run --rm \
  -v <volume-name>:/artifacts \
  -v /path/to/your/models:/src:ro \
  alpine sh -c 'mkdir -p /artifacts/embeddinggemma-2 /artifacts/minilm && \
    cp -r /src/embeddinggemma-2/* /artifacts/embeddinggemma-2/ && \
    cp -r /src/minilm/* /artifacts/minilm/'
```

Expected volume layout:

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
failure backoff. The monthly DevDocs sync is still manual — its
image-manifest parsing is an open gap in the crawler. Details and current
status:
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
- DevDocs image-manifest parsing is a stub; the monthly sync needs an
  explicit manifest or the parsing gap closed.
