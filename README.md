# new_DRUID — Enterprise Local-First RAG + MCP

> Self-hosted, multi-tenant document knowledge system. Ingest documents,
> review the uncertain pages, and search everything — through a web UI, a
> REST API, or 8 MCP tools your AI agent can call directly.
> **Offline-first.** Deploys to a **single NVIDIA H200**.

---

## What it is

| Pillar | Description |
|---|---|
| 📥 **Ingestion** | PDF/text/Office → page router (native pdftext vs. visual olmOCR 2) → canonical document tree → structural chunking → CPU embeddings |
| 🧠 **Retrieval** | Hybrid dense (EmbeddingGemma 2, 768-dim) + server-side BM25 + identifier match → RRF fusion → MiniLM rerank, all tenant-scoped |
| 🔌 **MCP server** | 8 FastMCP tools (Streamable HTTP + stdio) with JWT auth and role-gated mutations |
| 🖥️ **Web UI** | Upload, Documents, Review queue, Quarantine, Search, and Admin views — dark mode, no CDN, vendored htmx |

**Security model in one paragraph:** tenant identity comes only from a
validated JWT (never from document content, tool arguments, or headers);
retrieved text is wrapped in provenance delimiters and marked
`trust="untrusted-document-content"` so it can never steer the agent;
unreviewed, inactive, or deleting versions are hard-filtered at the catalog
layer and never surface in search. Fail closed, everywhere.

## Architecture

```
browser / AI agent / CLI
  │  web_ui :8005 (host)            mcp_server :8002 (host, Bearer JWT)
  ▼                                ▼
┌─────────────────────────────────────────────────────────┐
│ gateway — JWT validation, quotas, HMAC-signed identity   │  (internal)
└──────────────┬────────────────────────┬─────────────────┘
               ▼                        ▼
   document_api (upload /               mcp_server ──► retrieval (hybrid
   review / admin)                      8 tools        search, CPU)
        │                                              │
        ▼                                              ▼
   catalog (PostgreSQL)                         Milvus (dense+BM25)
        │                                              │
        ▼                                              ▼
   job queue (Redis/RQ) ──► ingestion workers ──► RustFS (S3)
        pdftext ──► page router ──► olmOCR 2 FP8 on vLLM (ONLY GPU consumer)
```

Backend containers remain on private Compose networks rather than joining
the external **`cora`** network. Web UI and MCP retain opt-in cross-project
reachability; ingestion also joins **`druid-net`** for GPU OCR.
Only two host ports are published: **8005** (web UI) and **8002** (MCP,
loopback-bound by default). Everything else is container-to-container.

## Quickstart

```bash
cp .env.example .env          # fill in secrets (never commit .env)
docker network create cora    # only if not already present on your host
docker network create druid-net # shared with the GPU OCR project

# 1. Infrastructure
docker compose --env-file .env -f deploy/docker-compose.yml up -d
# 2. Schema + search collections (one-shot, must exit 0)
docker compose --env-file .env -f deploy/docker-compose.yml run --rm catalog
docker compose --env-file .env -f deploy/docker-compose.yml run --rm milvus_setup
# 3. CPU services
docker compose --env-file .env -f deploy/docker-compose.yml up -d
# 4. GPU plane (H200 host only) — vLLM serving olmOCR-2-7B-FP8
docker compose --env-file .env -f deploy/docker-compose.gpu.yml up -d vllm-olmocr
```

After configuring Keycloak and HTTPS (see below), open the DRUID HTTPS
hostname. The login page redirects to Keycloak using Authorization Code + PKCE.
**Token-paste login is disabled in production.** DRUID group membership and
roles live in PostgreSQL and are managed with the Groups page or REST API,
not in Keycloak. A user can belong to multiple groups; the selected group
scopes uploads, searches, review and deletion. Members may remove only their
own uploaded documents; group managers may manage group documents.

> **First boot:** stage model artifacts and initialize the `druid-net`
> GPU network. Backend and MCP containers are private; externally exposed
> browsers and AI clients use the HTTPS reverse proxy in front of the UI
> and authenticated API gateway. `cora` is no longer required.
> See `docs/runbook.md` and `DRUID_HARDENING.md` before any rollout.

## AI skills for natural-language DRUID interaction

- [`skills/druid-natural-language/SKILL.md`](skills/druid-natural-language/SKILL.md) — load this when someone asks an AI in ordinary language to search DRUID, answer questions with document citations, upload a PDF, check ingestion, review a page, delete an owned document, or work in multiple groups. Includes accurate intent routing, the gateway API contract, scenario examples, and a safe standard-library client in `scripts/druid_user.py`.
- [`skills/druid-management/SKILL.md`](skills/druid-management/SKILL.md) — privileged companion for DRUID system-administrator REST actions: users, internal groups, memberships, quotas and document oversight.
- [`skills/druid-sage/SKILL.md`](skills/druid-sage/SKILL.md) — usage guide and project disambiguation for DRUID versus New_sage.

The AI needs an **authorized Keycloak bearer token and a real HTTPS DRUID
gateway connection**; installing a skill alone does not create a remote
connector or silently grant access. The user skill uses DRUID groups from
PostgreSQL and selects them per request, never Keycloak group claims.
Use documented APIs through the gateway, not internal service ports.

`DRUID_API_BASE` is an HTTPS origin and `DRUID_ACCESS_TOKEN` is a
short-lived access token supplied to the agent runtime by its approved
identity integration. Example user operations from a connected shell:

```bash
python skills/druid-natural-language/scripts/druid_user.py me
python skills/druid-natural-language/scripts/druid_user.py --group engineering search "What is our backup schedule?"
python skills/druid-natural-language/scripts/druid_user.py --group engineering upload ./handbook.pdf
python skills/druid-natural-language/scripts/druid_user.py --group engineering docs
```

All write operations remain subject to DRUID's API permissions and any
required human confirmation. The skill and examples are *not* a
deployed service; a live Keycloak/H200 integration test remains pending.

## MCP tools

| Tool | What it does | Role |
|---|---|---|
| `search_knowledge_base` | Hybrid search with provenance-tagged passages | any |
| `ingest_document` | Register upload → ticket → bytes → job | any |
| `get_ingestion_status` | Job state for an upload | any |
| `list_documents` / `get_document` | Tenant-scoped document metadata | any |
| `get_review_item` | Inspect a quarantined page | reviewer+ |
| `resolve_review_item` | Accept / re-route / reject a page | reviewer+ |
| `delete_document` | Full deletion (confirm-gated) | any |

```jsonc
// Claude Code / MCP client config (Streamable HTTP)
{ "mcpServers": { "druid": {
  "url": "https://druid.example.invalid/mcp",
  "headers": { "Authorization": "Bearer <Keycloak access JWT>" }
}}}
// …or local trusted stdio:
// python -m mcp_server.stdio_adapter --user-id you --group-id your-tenant --roles admin,reviewer
```

## REST API (behind the gateway)

| Area | Endpoints |
|---|---|
| Documents | `POST /v1/documents/register` → `POST /v1/documents/{id}/bytes` (ticketed upload), `GET /v1/documents`, `GET /v1/documents/{id}`, `DELETE /v1/documents/{id}` |
| Review | `GET /v1/review/items`, `GET …/{item_id}`, `POST …/{item_id}/resolve` |
| Jobs | `GET /v1/jobs/{job_id}` |
| System admin | `GET/POST/PATCH /v1/admin/users`, `GET/POST/PATCH /v1/admin/groups`, `PUT/DELETE /v1/admin/groups/{group}/members/{subject}`, `GET /v1/admin/documents`, `GET /v1/admin/jobs`, `GET /v1/admin/quotas` |
| Group context | `GET /v1/identity/me`, request header `X-Group-Selection: engineering` (verified against database membership) |

Two-phase upload exists so large files can be chunked/resumable (the web UI
splits in the browser); single-shot multipart upload also works.

## Offline-first & artifact policy

- Every model/dependency has a manifest in `services/artifacts/manifests/`
  (pinned upstream revision, sha256, license). Production loads **only**
  pre-staged artifacts and fails startup rather than downloading.
- `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` baked into every image.
- Approved models: `allenai/olmOCR-2-7B-1025-FP8`, `google/embeddinggemma-2`
  (text+image), `cross-encoder/ms-marco-MiniLM-L6-v2`. Object storage is
  **RustFS** (`rustfs/rustfs:1.0.1`, Apache-2.0).
- Prohibited: modelscope.cn, Chinese mirrors, unapproved endpoints.

## Repo layout

```
services/
  gateway/       JWT auth, quotas, trusted identity (HMAC-signed X-Druid-*)
  document_api/  Upload (ticketed), review queue, admin API
  mcp_server/    8 FastMCP tools — Streamable HTTP + stdio adapter
  web_ui/        FastAPI + Jinja2 + vendored htmx, dark-mode default
  catalog/       PostgreSQL models, Alembic, tenant-scoped repository
  ingestion/     pdftext router, olmOCR client, chunker, pipeline, RQ workers
  embedding/     EmbeddingGemma 2 CPU service (task-aware prefixes)
  retrieval/     Hybrid search: dense + BM25 + identifiers → RRF → MiniLM
  milvus_setup/ One-shot collection/index/alias job (pymilvus)
  artifacts/     Manifest validate/verify CLI + example manifests
deploy/          docker-compose.yml (CPU) · docker-compose.gpu.yml (vLLM)
docs/            runbook.md · backup-restore.md
docs-site/       18-page documentation site (sources + built HTML)
skills/          druid-sage AI skill: usage/UX knowledge for both projects
tests/           Cross-service wiring + deploy tests
```

## Configuration

All tunables live in `.env.example` (copy to `.env`). The highlights:

| Variable | Default | Purpose |
|---|---|---|
| `WEB_UI_PORT` / `MCP_PORT` | `8005` / `8002` | The only published host ports |
| `JWT_SECRET` or `JWT_PUBLIC_KEY_PEM` | — | Bearer-token verification (never commit) |
| `QUOTA_INGEST_PAGES_PER_DAY` / `QUOTA_QUERY_QPS` | `1000` / `10` | Per-tenant quotas (Redis) |
| `MODEL_DIR` | `/models` | Host artifact registry, mounted read-only |
| `MAX_UPLOAD_MB` / `MAX_PAGES_PER_DOCUMENT` | `200` / `2000` | Upload guards |

## Rust CPU experiment

The optional [PyO3 Rust retrieval fast path](docs/rust-fastpath.md) covers
only post-filter RRF score fusion and batched passage truncation. Measured
on GitHub Actions, **Rust was slower** than Python for representative
CPU microbenchmarks, so `DRUID_RUST_FASTPATH` remains **off** by default.
The regular images do not install the optional wheel, and Python/model
inference behavior is unchanged. The experiment is parity-tested in CI
without weakening PostgreSQL authorization or retrieval predicates.

## Testing

```bash
python -m pytest            # whole tree: 349 passed, 4 skipped (live-Milvus)
python -m pytest services/web_ui  # 32 passed (own venv)
```

Per-service suites run the same way from each `services/<name>/`
(the tree is unified — one invocation runs everything).

## Docs & skill

- 📖 **Full documentation site:** [`docs-site/site/index.html`](docs-site/site/index.html)
  (18 pages: architecture, MCP reference, web UI guide, runbook, operations…)
- 🤖 **`druid-sage` skill** ([`skills/druid-sage`](skills/druid-sage)): lets any AI
  answer usage/UX questions about new_DRUID (and New_sage) from these docs.

## Status

Built and tested; all pushes current on `main`. What still needs the H200:
model weights staged, full corpus ingestion, live Postgres/Redis/RustFS/Milvus
integration tests, and the M1 eval-corpus calibration (router thresholds,
rerank width). See `BUILD_LOG.md` for the full build history.
