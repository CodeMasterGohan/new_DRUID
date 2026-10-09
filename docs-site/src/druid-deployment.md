# new_DRUID — Deployment Runbook

Target: one machine with an NVIDIA H200 (141 GB HBM3e). Docker Engine +
Compose v2 + NVIDIA Container Toolkit. There are two compose projects;
both attach to the shared external `druid-net` network.

> **Not yet run:** bring-up, smoke tests, offline verification, quota
> burn-in, and the GPU arbitration check below are all documented in
> `docs/runbook.md` but have **not been executed** — they need the H200
> host. Model weights are not staged; dependency pins are initial
> lockfiles (regenerate with `pip-compile` before release).

## Bring-up order

```bash
cp .env.example .env        # fill secrets; never commit a filled .env

# 0. Shared network for cross-project DNS (ingestion workers -> vLLM).
#    Both compose projects attach to it as external. Create once per host.
docker network create druid-net

# 1. Infrastructure
docker compose -f deploy/docker-compose.yml up -d postgres redis minio etcd milvus

# 2. Schema + migrations (one-shot jobs, must exit 0)
docker compose -f deploy/docker-compose.yml run --rm catalog        # alembic upgrade head
docker compose -f deploy/docker-compose.yml run --rm milvus_setup   # collections + indexes

# 3. CPU services
docker compose -f deploy/docker-compose.yml up -d gateway document_api mcp_server embedding retrieval
docker compose -f deploy/docker-compose.yml up -d --scale ingestion=4 ingestion

# 4. GPU plane (H200 host only)
docker compose -f deploy/docker-compose.gpu.yml up -d vllm-olmocr
curl -f http://localhost:8000/health   # vLLM health (VLLM_PORT)
```

## Compose projects

| Project | File | Services | Notes |
|---|---|---|---|
| `druid` (CPU) | `deploy/docker-compose.yml` | postgres, redis, minio, etcd, milvus, one-shot catalog + milvus_setup, gateway, document_api, mcp_server, web_ui, embedding, retrieval, ingestion (scalable) | Container images are `python:3.11-slim` builds from each service dir |
| `druid-gpu` | `deploy/docker-compose.gpu.yml` | vllm-olmocr | nvidia device reservation, count 1 |

Notable wiring (all verified by `tests/test_deploy_wiring.py`):

- `druid-net` is declared **external** in both files and attached to
  `ingestion` and `vllm-olmocr` so workers resolve `vllm-olmocr` by
  service name.
- Gateway host port is **8080** (`GATEWAY_PORT`; container still listens
  on 8000) because vLLM binds host port 8000 on the same machine.
  Compose service names use underscores (`document_api`,
  `mcp_server`) — gateway defaults were corrected to match.
- `embedding` and `retrieval` mount `${MODEL_DIR:-/models}:/models:ro`
  (read-only); `ingestion` mounts the same.

## GPU arbitration (§2)

**vLLM serving olmOCR is the ONLY GPU consumer.** Embedding
(EmbeddingGemma 2) and the MiniLM reranker run on CPU. Any additional GPU
consumer requires re-validation of this policy.

- Checkpoint: `allenai/olmOCR-2-7B-1025-FP8` (FP8 is the approved
  production checkpoint; the BF16 `olmOCR-2-7B-1025` is approved for
  fine-tuning only).
- Launch: `--served-model-name olmocr-2-fp8 --gpu-memory-utilization 0.7
  --max-model-len 8192 --limit-mm-per-prompt image=4`,
  `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`.
- HBM3e math (141 GB): FP8 weights ≈ 7 GB; vLLM reservation at 0.7 ≈
  98.7 GB for weights + KV cache + activations; ≈ 42 GB deliberately
  unallocated headroom.
- `--gpu-memory-utilization 0.7` is the **initial** cap — tune against
  KV-cache pressure under concurrent page renders. If OOM under burst,
  the lever is **ingestion concurrency** (worker count, quotas), not
  lowering the cap and not a second GPU consumer. Measure olmOCR
  pages/hour **while** embedding/query services are under load (§13).

## Model staging (artifact manifests)

Before any service starts, the host registry at `$MODEL_DIR` (default
`/models`) must contain the three approved checkpoints, each completed
per its manifest in `services/artifacts/manifests/`:

- `allenai/olmOCR-2-7B-1025-FP8`
- `google/embeddinggemma-2`
- `cross-encoder/ms-marco-MiniLM-L6-v2`

Manifests carry the pinned upstream revision, sha256 checksums, and
license; `services/artifacts` provides `validate` / `verify` CLI
commands. Manifests in `manifests/` were templates (zeroed sha256,
unfilled revision); completed versions live in `manifests/completed/`.

## Offline-first startup behavior

All images set `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`. Missing
artifacts **fail startup loudly and name the artifact and expected mount
path** — they never download at runtime. Offline verification
(`docs/runbook.md` §4): with external network blocked, every service
must start cleanly and serve.

## Smoke tests (post bring-up)

```bash
# Gateway rejects unauthenticated requests
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/healthz   # expect 401/403

# Milvus collection exists with the alias
# via milvus_setup logs or: utility.has_collection("project_knowledge_base_v1")

# Embedding service loads the staged model read-only (no download)
docker compose logs embedding | grep -i "offline\|model loaded"

# vLLM serves the FP8 checkpoint
curl http://localhost:8000/v1/models   # expect served-model-name olmocr-2-fp8
```

## Host sizing (from runbook §1 — record, don't assume)

- Per text chunk ≈ **8 KB** host RAM (dense 768-dim float32 3.0 KB + HNSW
  ~0.5–1.0 KB + BM25 ~1–2 KB + metadata ~2–4 KB); per page-image record
  ≈ 4 KB.
- Worked example (5M chunks + 200K page images): Milvus ≈ 43 GB,
  PostgreSQL ≈ 5 GB, MinIO sized from corpus × 2 headroom, vLLM host-side
  ≈ 4 GB, OS page cache + headroom ≈ 16 GB → **≥ 96 GB host RAM
  (128 GB comfortable)**. GPU memory is not part of this budget.
- Scaling rule of thumb: ~8 KB host RAM per text chunk (10M chunks ≈
  80 GB Milvus-side). Beyond host RAM, the options are a bigger host,
  Matryoshka truncation (after M1 validation), or sharding tenants
  across hosts (out of v1 scope).

## Quota burn-in (before production traffic)

1. Ingest a representative batch; measure pages/hour against the H200.
2. Validate `QUOTA_INGEST_PAGES_PER_DAY=1000` is sustainable; adjust.
3. Load-test query QPS against the retrieval service; validate
   `QUOTA_QUERY_QPS=10`.
4. Record measured numbers. Quotas are estimates until this is done.
