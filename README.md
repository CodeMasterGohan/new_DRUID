# new_DRUID — Enterprise Local-First RAG + MCP

Self-hosted, multi-tenant document knowledge system. Ingestion, management, and
retrieval are exposed through an authenticated API and an MCP server.
**Offline-first**: production services load pre-staged artifacts and talk only
to approved internal services. Deployment target: a **single NVIDIA H200**.

The normative architecture document is
[`../new_DRUID_architecture_v2.md`](../new_DRUID_architecture_v2.md) (spec v2.1).
This repo is its implementation. When the two disagree, the spec wins and this
repo gets fixed.

## Architecture (condensed)

```
clients (Web UI / AI agent / CLI)
  → API Gateway (token validation, quotas, trusted identity context)
      → Document API (upload / status / list / delete / review queue)
      → MCP Server (FastMCP, Streamable HTTP + stdio adapter, 8 tools)
  → Document Catalog (PostgreSQL: ownership, versions, jobs, review quarantine)
  → Job Queue (Redis-backed; durable ingestion/deletion work)
  → Ingestion Workers (pdftext native path → page router → olmOCR 2 FP8 →
     canonical document tree → structural chunking → EmbeddingGemma 2 on CPU)
  → Milvus (dense 768-dim + BM25 hybrid, group_id partition key)
  → Object Storage (RustFS, S3-compatible, tenant-scoped: originals / renders / trees)
GPU: vLLM serves olmOCR-2-7B-FP8 — the ONLY GPU consumer (--gpu-memory-utilization 0.7)
```

Key policies:

- **Tenant context** comes only from validated auth/gateway context — never from
  document content, tool arguments, or headers. Fail closed.
- **No partial visibility**: inactive, deleting, failed, or unreviewed versions
  are hard-filtered at the catalog layer, never returned as searchable.
- **Retrieved text is untrusted**: provenance-tagged responses; content can never
  alter identity, authorization, or instructions.
- **Approved artifacts only**: every model/dependency has a manifest
  (`services/artifacts/manifests/`) with pinned revision, sha256, license.
  ModelScope, Chinese mirrors, and unapproved endpoints are prohibited.

## Repo layout

```
services/
  gateway/        Auth gateway (FastAPI)
  document_api/   Document + review-queue API (FastAPI)
  mcp_server/     FastMCP server (Streamable HTTP + stdio)
  catalog/        PostgreSQL models + Alembic migrations (library + runner)
  ingestion/      pdftext workers, page router, chunking, embedding client
  embedding/      EmbeddingGemma 2 CPU service (text+image, task-aware prompts)
  retrieval/      Hybrid search, RRF fusion, MiniLM rerank (CPU)
  milvus_setup/   One-shot collection/index creation job (pymilvus)
  artifacts/      Artifact manifest tooling (validate / verify / allowlist)
deploy/
  docker-compose.yml        Postgres, Redis, RustFS, Milvus standalone
  docker-compose.gpu.yml    vLLM olmOCR-2-7B-FP8 on the H200
  vllm-olmocr.md            vLLM config + GPU arbitration notes
docs/
  runbook.md                Deployment runbook + host-sizing math
  backup-restore.md         Backup/restore procedure (per §13 acceptance)
```

## Quickstart (dev)

```bash
cp .env.example .env
# 1. Start infrastructure
docker compose -f deploy/docker-compose.yml up -d
# 2. Run migrations + create Milvus collections
docker compose -f deploy/docker-compose.yml run --rm catalog
docker compose -f deploy/docker-compose.yml run --rm milvus_setup
# 3. Start services
docker compose -f deploy/docker-compose.yml up -d gateway document_api mcp_server
# 4. GPU node only: start olmOCR inference
docker compose -f deploy/docker-compose.gpu.yml up -d vllm-olmocr
```

## Dependency pins

Each service has a `requirements.txt` with `==` pins (Python 3.11+).
Pins were chosen 2026-10-08 — **regenerate with `pip-compile` before any
release**; treat them as a starting lockfile, not a verified one.

## Offline operation

All service images set `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`.
Models mount read-only from the host artifact registry
(`MODEL_DIR`, default `/models`). Missing artifacts fail startup loudly —
never download at runtime.

## M1 eval corpus

`eval/` (not yet built) will hold the 50–100 labeled documents that gate the
page router, chunking targets, and retrieval tuning. Nothing ships untuned.
