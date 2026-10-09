# new_DRUID — Configuration Reference

Every variable below is present in `build/.env.example`. Copy it to
`.env` and fill in secrets — **never commit a filled `.env`**. Service
names follow the shared template without a `DRUID_` prefix (a deliberate
convention, aligned across gateway, MCP server, and siblings).

## PostgreSQL (catalog)

| Variable | Default | Notes |
|---|---|---|
| `POSTGRES_USER` | `druid` | |
| `POSTGRES_PASSWORD` | `change-me` | **Secret** |
| `POSTGRES_DB` | `druid_catalog` | |
| `DATABASE_URL` | `postgresql+psycopg://druid:change-me@postgres:5432/druid_catalog` | `alembic/env.py` requires this and refuses to guess |

Image: `postgres:16.6-alpine`.

## Redis (queue broker + cache)

| Variable | Default | Notes |
|---|---|---|
| `REDIS_URL` | `redis://redis:6379/0` | Job queue broker; quota counters; gateway uses it for quotas when set, in-memory otherwise |

Image: `redis:7.4.2-alpine` with `--appendonly yes`.

## RustFS (S3-compatible object storage)

| Variable | Default | Notes |
|---|---|---|
| `RUSTFS_ACCESS_KEY` | `druid` | |
| `RUSTFS_SECRET_KEY` | `change-me-min-8-chars` | **Secret** (min 8 chars) |
| `RUSTFS_ENDPOINT` | `rustfs:9000` | |
| `RUSTFS_BUCKET` | `druid-artifacts` | |

Milvus shares this RustFS (separate bucket) instead of running its own.
Image is `rustfs/rustfs:1.0.1` (pinned). The `minio` Python client works
against it unmodified.

## Milvus

| Variable | Default | Notes |
|---|---|---|
| `MILVUS_URI` | `http://milvus:19530` | |
| `MILVUS_COLLECTION` | `project_knowledge_base_v1` | |
| `MILVUS_ALIAS` | `project_knowledge_base` | |

Image: `milvusdb/milvus:v2.5.6` (standalone) + `quay.io/coreos/etcd:v3.5.16`.

## Auth (gateway)

| Variable | Default | Notes |
|---|---|---|
| `JWT_ISSUER` | `https://auth.internal.example` | Expected `iss`; MCP server default is `druid-gateway` |
| `JWT_AUDIENCE` | `druid-api` | Expected `aud`; MCP server default is `druid-mcp` |
| `JWKS_URL` | `https://auth.internal.example/.well-known/jwks.json` | For RS256/ES256 token verification |

New_DRUID has **no credential store**; tokens are provisioned out-of-band
by the operator (see [Web UI](druid-web-ui.html)).

## Quotas (§4)

| Variable | Default | Notes |
|---|---|---|
| `QUOTA_INGEST_PAGES_PER_DAY` | `1000` | Per-tenant daily page budget; estimates until burn-in |
| `QUOTA_QUERY_QPS` | `10` | Per-tenant sliding-window QPS |
| `MAX_UPLOAD_MB` | `200` | Gateway returns 413 over cap |
| `MAX_PAGES_PER_DOCUMENT` | `2000` | 400 over cap |

## Model artifacts (mounted read-only from host registry)

| Variable | Default | Notes |
|---|---|---|
| `MODEL_DIR` | `/DATA/docker/mounts/models` | Host artifact registry; mounted `:ro` into embedding, retrieval, ingestion, vLLM |
| `OLMOCR_MODEL_DIR` | `/DATA/docker/mounts/models/allenai/olmOCR-2-7B-1025-FP8` | FP8 production checkpoint |
| `EMBEDDING_MODEL_DIR` | `/DATA/docker/mounts/models/google/embeddinggemma-2` | Fallback alias; see next row |
| `EMBEDDING_MODEL_PATH` | `/DATA/docker/mounts/models/google/embeddinggemma-2` | **Primary** name the embedding service reads |
| `EMBEDDING_MODALITY_CONFIG` | `text+image` | `text+image` \| `text-only` |
| `EMBEDDING_DIMENSION` | `768` | Validated Matryoshka widths: 768/512/256/128 |
| `EMBEDDING_BACKEND` | `st` | `st` (sentence-transformers) \| `mock` |
| `RERANKER_MODEL_DIR` | `/DATA/docker/mounts/models/cross-encoder/ms-marco-MiniLM-L6-v2` | MiniLM reranker |
| `MINILM_TOKENIZER_PATH` | `/DATA/docker/mounts/models/cross-encoder/ms-marco-MiniLM-L6-v2` | Tokenizer the ingestion chunker enforces its token budget with (fail-closed); separate var because the chunker needs only the tokenizer |
| `HF_HUB_OFFLINE` | `1` | Baked into images; never download at runtime |
| `TRANSFORMERS_OFFLINE` | `1` | Baked into images |

## vLLM / olmOCR (§2 GPU arbitration)

| Variable | Default | Notes |
|---|---|---|
| `OLMOCR_VLLM_URL` | `http://vllm-olmocr:8000` | Base URL of the vLLM OpenAI-compatible endpoint; read by ingestion workers; resolves via shared `druid-net` (container-to-container; no host port) |
| `VLLM_GPU_MEMORY_UTILIZATION` | `0.7` | Initial cap; tune against KV-cache pressure |
| `VLLM_MAX_MODEL_LEN` | `8192` | |

## Service ports

Host-port policy (2026-10-09): only `WEB_UI_PORT` (8005, browser) and
`MCP_PORT` (8002, AI CLI) publish host ports. Everything else is
container-to-container — the remaining `*_PORT` variables were removed from
`.env.example`.

| Variable | Default | Notes |
|---|---|---|
| `MCP_PORT` | `8002` | Host mapping for the AI CLI |
| `WEB_UI_PORT` | `8005` | Host mapping for browser access |

Container-internal ports: gateway 8000, document_api 8001, embedding 8003,
retrieval 8004, vLLM 8000, RustFS 9000/9001, Milvus 19530/9091.

## Service URLs (compose DNS — service names use underscores)

| Variable | Default | Notes |
|---|---|---|
| `DOCUMENT_API_URL` | `http://document_api:8001` | Gateway upstream |
| `MCP_SERVER_URL` | `http://mcp_server:8002` | Gateway upstream |
| `GATEWAY_URL` | `http://gateway:8000` | Container-internal DNS (web_ui hardcodes this in compose; env default here) |

## Internal identity propagation (gateway → backend services)

| Variable | Default | Notes |
|---|---|---|
| `INTERNAL_SECRET` | `<redacted>` (template ships empty; gateway's own default is `change-me-in-production`) | HMAC secret the gateway uses to sign `X-Druid-*` identity headers. **Secret. Never commit a filled value.** |

## MCP server (Streamable HTTP, authenticated)

| Variable | Default | Notes |
|---|---|---|
| `JWT_SECRET` | `change-me` | HS256 shared secret for bearer-token verification. **One of `JWT_SECRET` or `JWT_PUBLIC_KEY_PEM` is required. Secret.** |
| `JWT_PUBLIC_KEY_PEM` | (unset) | RS256 public key alternative |
| `MCP_ALLOWED_ORIGINS` | (empty) | Comma-separated origins allowed to present an `Origin` header (DNS-rebinding protection). Requests without `Origin` (native clients) are allowed; with a non-allowlisted `Origin` → 403 |

Also read by the MCP server: the shared `JWT_ISSUER` / `JWT_AUDIENCE`
names (not `DRUID_`-prefixed), and `DRUID_MCP_DEV_FAKE_CLIENTS=1`
(dev/fake clients — compose-only, never real tenant data).

## Notes

- `COOKIE_SECURE` (default `true`) exists in the web_ui README config
  table — set `false` for plain-HTTP local dev.
- The web_ui uploads through the gateway, so `MAX_UPLOAD_MB` applies
  there too; the UI uses its own `max_upload_mb` (200) and
  `upload_chunk_mb` (8) settings.
- All `${VAR}` references in the compose files have a template entry or
  a default (verified by `tests/test_deploy_wiring.py`).
