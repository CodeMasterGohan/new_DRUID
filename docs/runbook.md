# Deployment runbook — new_DRUID on a single H200 host

Target: one machine with an NVIDIA H200 (141 GB HBM3e). All services except
vLLM run on CPU/host RAM. vLLM (olmOCR-2-7B-FP8) is the ONLY GPU consumer
(spec §2).

## 0. Prerequisites

- Docker Engine + Compose v2, NVIDIA Container Toolkit.
- Host artifact registry at `$MODEL_DIR` (default
  `/DATA/docker/mounts/models`) containing the three approved checkpoints,
  each completed per its artifact manifest
  (`services/artifacts/manifests/`):
  - `allenai/olmOCR-2-7B-1025-FP8`
  - `google/embeddinggemma-2`
  - `cross-encoder/ms-marco-MiniLM-L6-v2`
- `.env` filled from `.env.example`. No external network at runtime.

### Host bind-mount directories

All persistent data lives in bind mounts under `/DATA/docker/mounts/druid/<service>`
— no named volumes. Docker creates a missing bind-mount directory as **root**,
so pre-create them and fix ownership for the images that don't run as root:

```bash
sudo mkdir -p /DATA/docker/mounts/druid/{postgres,redis,rustfs,etcd,milvus}
sudo mkdir -p /DATA/docker/mounts/models
sudo chown 70:70    /DATA/docker/mounts/druid/postgres   # postgres:16-alpine runs as UID 70
sudo chown 999:999  /DATA/docker/mounts/druid/redis      # redis:7-alpine runs as UID 999
sudo chown 10001:10001 /DATA/docker/mounts/druid/rustfs   # rustfs runs as UID 10001
# etcd and milvus run as root — no chown needed. models/ is read-only
# (stage the checkpoints there per the manifests above).
```

## 1. Host sizing (spec §9 — record the math, don't assume it)

The H200's 141 GB is **GPU memory**. Milvus, PostgreSQL, RustFS, and the vLLM
host-side footprint all live in **host RAM**. Size host RAM and disk from the
target corpus:

### Per-chunk / per-record costs

| Item | Math | Per unit |
|---|---|---|
| Dense vector (768-dim float32) | 768 × 4 B | **3.0 KB** |
| HNSW graph overhead (M=16) | ≈ 2·M links × 4 B + node overhead | **~0.5–1.0 KB** |
| Sparse BM25 vector (variable) | corpus-dependent | **~1–2 KB** (measure on M1) |
| Page-image record (dense only) | same 768-dim math | **~3.5 KB** |
| Metadata / identifiers / text (Milvus) | row-dependent | **~2–4 KB** |

All-in planning figure: **~8 KB per text chunk**, **~4 KB per page-image record**.

### Worked example — 5M chunks + 200K page images

| Store | Calculation | Size |
|---|---|---|
| Milvus dense+HNSW | 5M × 4 KB | ~20 GB |
| Milvus sparse BM25 | 5M × 1.5 KB | ~7.5 GB |
| Milvus page images | 200K × 4 KB | ~0.8 GB |
| Milvus row overhead/text | 5.2M × 3 KB | ~15 GB |
| **Milvus total** | | **~43 GB** |
| PostgreSQL (catalog: docs, versions, jobs) | metadata only | ~5 GB |
| RustFS (originals + renders + trees + markdown) | corpus-dependent, disk-bound | size from corpus × 2 headroom |
| vLLM host-side (tokenizer, misc) | | ~4 GB |
| OS page cache + headroom | | ~16 GB |
| **Host RAM target** | | **≥ 96 GB (128 GB comfortable)** |

Disk: NVMe, sized to the corpus originals + derived artifacts with 2×
headroom. GPU memory is NOT part of this budget — see `deploy/vllm-olmocr.md`
for the separate 141 GB HBM3e math.

### Scaling rule of thumb

~8 KB host RAM per text chunk → 10M chunks ≈ 80 GB Milvus-side. If the corpus
plan exceeds host RAM, the choices are: bigger host, Matryoshka truncation
(§8, after M1 validation), or sharding tenants across hosts (out of v1 scope).

## 2. Bring-up order

```bash
cp .env.example .env   # fill secrets

# 0. Shared network for cross-project DNS (ingestion workers → vLLM).
#    Both compose projects attach to it as external. Create once per host.
docker network create druid-net

# 1. Infrastructure
docker compose -f deploy/docker-compose.yml up -d postgres redis rustfs etcd milvus

# 2. Schema + migrations (one-shot jobs, must exit 0)
docker compose -f deploy/docker-compose.yml run --rm catalog        # alembic upgrade head
docker compose -f deploy/docker-compose.yml run --rm milvus_setup   # collections + indexes

# 3. CPU services
docker compose -f deploy/docker-compose.yml up -d gateway document_api mcp_server embedding retrieval
docker compose -f deploy/docker-compose.yml up -d --scale ingestion=4 ingestion

# 4. GPU plane (H200 host only)
docker compose -f deploy/docker-compose.gpu.yml up -d vllm-olmocr
docker compose -f deploy/docker-compose.gpu.yml exec vllm-olmocr curl -f http://localhost:8000/health   # vLLM health (internal-only port)
```

## 3. Smoke tests

```bash
# Gateway rejects unauthenticated requests (gateway port is internal-only;
# reach it via the compose network)
docker compose -f deploy/docker-compose.yml exec gateway curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/healthz   # expect 401/403 without token

# Milvus collection exists with the alias
# (via milvus_setup logs or pymilvus: utility.has_collection("project_knowledge_base_v1"))

# Embedding service loads the staged model read-only (no download)
docker compose logs embedding | grep -i "offline\|model loaded"

# vLLM serves the FP8 checkpoint
curl http://localhost:8000/v1/models   # expect served-model-name olmocr-2-fp8
```

## 4. Offline verification (§3 step 7)

With external network blocked (firewall or `docker network disconnect`), every
service must start cleanly and serve. Expected behavior:

- Embedding / retrieval / ingestion: start, log model load from `/models`.
- vLLM: starts from the mounted checkpoint.
- Any `MissingArtifact` / startup failure must name the artifact and the
  expected mount path — never attempt a download.

## 5. Quota burn-in (§4, open decision #2)

Before production traffic, run a throughput burn-in:

1. Ingest a representative batch; measure pages/hour against the H200.
2. Validate `QUOTA_INGEST_PAGES_PER_DAY=1000` is sustainable; adjust.
3. Load-test query QPS against the retrieval service; validate `QUOTA_QUERY_QPS=10`.
4. Record measured numbers here. Quotas are estimates until this is done.

## 6. GPU arbitration check (§13)

Measure olmOCR pages/hour **while** the embedding and query services are under
load. If KV-cache pressure forces the 0.7 cap down, the lever is ingestion
concurrency (worker count, quotas) — not a second GPU consumer.
