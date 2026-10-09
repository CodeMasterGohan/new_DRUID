# Deployment cheat-sheet (new_DRUID + New_sage)

Both target a single NVIDIA H200 host. New_sage is CPU-only; new_DRUID uses the
GPU for exactly one service (vLLM serving olmOCR).

## Shared principles

- **Offline-first.** Production services load pre-staged artifacts from mounted
  local paths and fail startup when an asset is missing or invalid — they never
  download at runtime. Images bake in `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`.
- **Artifact manifests.** Every model/dependency ships with a manifest: pinned
  upstream revision, sha256 checksums, license, approval record.
  Downloads from `modelscope.cn`, Chinese mirror repositories, or other
  unapproved endpoints are prohibited.
- **`.env` comes from `.env.example`** — fill secrets there; nothing secret is
  committed.

## new_DRUID bring-up order

```bash
cp .env.example .env            # fill secrets
docker network create druid-net # once per host; shared by CPU + GPU compose projects

# 1. Infrastructure
docker compose -f deploy/docker-compose.yml up -d postgres redis rustfs etcd milvus

# 2. One-shot schema/setup jobs (must exit 0)
docker compose -f deploy/docker-compose.yml run --rm catalog        # alembic upgrade head
docker compose -f deploy/docker-compose.yml run --rm milvus_setup   # collections + indexes

# 3. CPU services
docker compose -f deploy/docker-compose.yml up -d gateway document_api mcp_server embedding retrieval
docker compose -f deploy/docker-compose.yml up -d --scale ingestion=4 ingestion

# 4. GPU plane (H200 host only) — the ONLY GPU consumer
docker compose -f deploy/docker-compose.gpu.yml up -d vllm-olmocr
```

Verify: `docker compose logs embedding | grep -i "offline\|model loaded"` (model
loaded read-only, no download); gateway health/ auth checks; Milvus collection
`project_knowledge_base_v1` with alias `project_knowledge_base` exists.

## new_DRUID port map

Host-port policy (2026-10-09): only `web_ui` (8005, browser) and `mcp_server`
(8002, AI CLI) publish host ports. Everything else is container-to-container
(container ports: gateway 8000, document_api 8001, embedding 8003, retrieval
8004, vLLM 8000, RustFS 9000/9001, Milvus 19530/9091).

**Superseded 2026-10-09:** the gateway no longer publishes a host port (was
8080); smoke checks run via `docker compose exec`.

## GPU arbitration (new_DRUID)

- Exactly one GPU consumer: **vLLM serving `allenai/olmOCR-2-7B-1025-FP8`**
  (served-model-name `olmocr-2-fp8`), launched with `--gpu-memory-utilization 0.7`
  (initial cap; tune against KV-cache pressure under concurrent page renders).
  The BF16 checkpoint is approved for fine-tuning only, never serving.
- Everything else — EmbeddingGemma 2 service (text+image config), MiniLM
  reranker, ingestion workers, Milvus, API services — runs on **CPU/host RAM**.
- The H200's 141 GB is GPU memory. Milvus/Postgres/RustFS/vLLM host-side all live
  in host RAM: plan **≥96 GB host RAM** (128 GB comfortable) and NVMe sized to
  the corpus ×2. Rough rule: ~8 KB host RAM per text chunk, ~4 KB per
  page-image record.
- Arbitration check before production: measure olmOCR pages/hour **while**
  embedding and query services are under load. If KV-cache pressure forces the
  0.7 cap down, reduce ingestion concurrency (worker count, quotas) — never add
  a second uncontended GPU consumer.

## Model staging (both projects)

Stage into `$MODEL_DIR` (default `/DATA/docker/mounts/models`), read-only bind
mounts in compose. All persistent data (not just models) lives in bind mounts
under `/DATA/docker/mounts/<service>` — no named volumes anywhere:

- new_DRUID: `allenai/olmOCR-2-7B-1025-FP8`, `google/embeddinggemma-2`,
  `cross-encoder/ms-marco-MiniLM-L6-v2`.
- New_sage: `google/embeddinggemma-2`, `cross-encoder/ms-marco-MiniLM-L6-v2`
  (no olmOCR — no GPU requirement; PDF manuals use the CPU-only pdftext fallback).
- Validate each with the artifact-manifest tooling (`validate`/`verify`) before
  bringing services up. Startup must succeed with external network blocked
  (test by blocking egress — any download attempt at runtime is a bug).

## New_sage specifics

- One compose stack; deploy decision (James, 2026-10-08): run on the H200 box
  alongside new_DRUID — tiny CPU-only footprint, one box to manage.
- **No auth** (bind localhost/Tailscale interface only); Origin check only.
- Default host ports: mcp_server `8010` (container 8000), retrieval `8011`
  (container 8001), embedding `8012` (container 8002), RustFS `9010`/`9011`
  (container 9000/9001), Milvus `19531`/`9092` (container 19530/9091),
  postgres `5433` (container 5432), redis `6380` (container 6379).
  Container ports are the §13 contract and are unchanged.
- **Resolved 2026-10-09:** New_sage's default host ports used to collide
  with new_DRUID's (both defaulted mcp to host 8000, etc.). They were moved
  to the 8010/9010/19531 block, so the two stacks no longer collide on the
  same box. `${*_HOST_PORT}` still overrides any of them in `.env`.
- Maintenance is fully automated by design: weekly refresh sweep + monthly
  DevDocs image sync are scheduled jobs in the stack — nothing manual.
