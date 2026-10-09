# New_sage Configuration Reference

Every variable below was found in the reviewed sources (`deploy/.env.example`,
`deploy/docker-compose.yml`, or a service's `settings.py`/`config.py`/README).
Vars not shown here are not documented — nothing is invented. See
[sage-deployment.html](sage-deployment.html) for how the stack is brought up.

## Global (`.env.example`)

Copy to `.env` to change secrets or remap host ports. Container ports are
fixed; these change only the host side (e.g. when colocating with new_DRUID).

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `sage` / `sage` / `sage` | Catalog Postgres credentials. **Change the passwords.** |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | `sage` / `sage-minio` | MinIO root credentials. **Change the password.** |
| `MILVUS_COLLECTION` | `sage_chunks` | Milvus collection name |
| `MILVUS_WAIT_TIMEOUT_S` | `600` | How long `milvus_setup` waits for Milvus |
| `MODEL_PATH` | `/artifacts/embeddinggemma-2` | EmbeddingGemma 2 checkpoint in the model-artifacts volume |
| `MINILM_MODEL_PATH` | `/artifacts/minilm` | MiniLM reranker checkpoint |
| `MINILM_TOKENIZER_PATH` | `/artifacts/minilm` | MiniLM tokenizer (defaults to the model path in retrieval) |
| `MCP_HOST_PORT` | `8000` | Host port for mcp-server |
| `RETRIEVAL_HOST_PORT` | `8001` | Host port for retrieval |
| `EMBEDDING_HOST_PORT` | `8002` | Host port for embedding |
| `MILVUS_HOST_PORT` / `MILVUS_MGMT_HOST_PORT` | `19530` / `9091` | Host ports for Milvus |
| `MINIO_HOST_PORT` / `MINIO_CONSOLE_HOST_PORT` | `9000` / `9001` | Host ports for MinIO |
| `REDIS_HOST_PORT` | `6379` | Host port for Redis |
| `POSTGRES_HOST_PORT` | `5432` | Host port for Postgres |

## mcp-server

| Variable | Default | Purpose |
|---|---|---|
| `MCP_HOST` | `0.0.0.0` | Bind address inside the container |
| `MCP_PORT` | `8000` | Container port (part of the port contract) |
| `RETRIEVAL_URL` | `http://retrieval:8001` | Retrieval service base URL |
| `DATABASE_URL` | (required) | Catalog Postgres, `postgresql+psycopg://…` |
| `REDIS_URL` | (required) | `redis://redis:6379/0` |
| `MCP_ALLOWED_ORIGINS` | (empty) | Comma-separated browser origins allowed past the Origin check (DNS-rebinding protection, not auth) |
| `SAGE_RESOLVER_TABLE` | `services/mcp_server/src/mcp_server/libraries_seed.json` | Resolver table path (hand-written seed table in the build) |
| `SAGE_MCP_DEV_FAKE_CLIENTS` | (unset) | Set to `1` for dev-mode fake retrieval clients |

## retrieval

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | (required) | Catalog Postgres |
| `EMBEDDING_URL` | `http://embedding:8002` | Embedding service |
| `MILVUS_URI` | `http://milvus:19530` | Milvus (compose sets `http://milvus-standalone:19530`) |
| `MILVUS_COLLECTION` | `sage_chunks` | Collection name |
| `MILVUS_TOKEN` | (unset) | Milvus auth token, optional |
| `MINILM_MODEL_PATH` | (required) | Pre-staged MiniLM cross-encoder directory |
| `MINILM_TOKENIZER_PATH` | = `MINILM_MODEL_PATH` | Pre-staged tokenizer directory |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | `1` | Enforced in compose; no model downloads ever |
| `RETRIEVAL_DENSE_CANDIDATES` | `50` | Dense-branch candidates |
| `RETRIEVAL_LEXICAL_CANDIDATES` | `50` | BM25-branch candidates |
| `RETRIEVAL_IDENTIFIER_CANDIDATES` | `20` | Identifier-branch candidates |
| `RETRIEVAL_OVERVIEW_CANDIDATES` | `50` | Overview-mode candidates |
| `RETRIEVAL_RERANK_CANDIDATES` | `50` | MiniLM rerank width (§17 decision: 50) |
| `RETRIEVAL_MAX_TOP_K` | `100` | Hard cap on requested `top_k` |
| `RETRIEVAL_RRF_K` | `60` | RRF damping constant |
| `RETRIEVAL_IDENTIFIER_BOOST` | `1.0` | Fixed additive boost for identifier-match hits |
| `EMBEDDING_DIMENSION` | `768` | Expected embedding width; query vectors are validated against it |

All tuning knobs are starting points for the §16 eval gate — change them only
with eval evidence.

## embedding

Settings use the `EMBEDDING_` prefix (`settings.py`); `MODEL_PATH` and
`EMBEDDING_MODEL_DIR` are accepted as aliases for the model path.

| Variable | Default | Purpose |
|---|---|---|
| `MODEL_PATH` | (required) | Pre-staged checkpoint dir (aliases: `EMBEDDING_MODEL_PATH`, `EMBEDDING_MODEL_DIR`) |
| `EMBEDDING_MODEL_ID` | `google/embeddinggemma-2` | Model identifier for bookkeeping |
| `EMBEDDING_MODALITY_CONFIG` | `text+image` | Checkpoint modality config (runtime selection; one shipped checkpoint) |
| `EMBEDDING_DIMENSION` | `768` | Must be a validated Matryoshka width: 768, 512, 256, or 128 |
| `EMBEDDING_NORMALIZE` | `true` | L2-normalize output vectors |
| `EMBEDDING_BACKEND` | `st` | `st` (sentence-transformers production) or `mock` (tests/dev) |
| `EMBEDDING_BATCH_SIZE` | `32` | Inference batch size (1–256) |
| `EMBEDDING_MAX_CONCURRENCY` | `4` | Request concurrency (1–32) |
| `EMBEDDING_MAX_INPUT_CHARS` | `32768` | Max input chars per request |
| `EMBEDDING_PORT` | `8002` | Container port |

`HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` are set in compose. CPU-only by
design; the service must never request GPU resources.

## ingestion-worker

| Variable | Default | Purpose |
|---|---|---|
| `REDIS_URL` | (required) | RQ transport, `sage-ingest` queue |
| `DATABASE_URL` | (required) | Catalog Postgres |
| `MILVUS_URI` | (required) | Milvus |
| `EMBEDDING_URL` | (required) | Embedding service |
| `MINIO_ENDPOINT` | (required) | Snapshot store |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | (required) | MinIO auth |

## crawler-worker

| Variable | Default | Purpose |
|---|---|---|
| `REDIS_URL` | (required) | RQ transport, `sage-crawl` queue |
| `DATABASE_URL` | (required) | Catalog Postgres |
| `MINIO_ENDPOINT` | (required) | Snapshot store |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | required for real MinIO | Snapshot store auth (compose maps the MinIO root credentials) |
| `MINIO_BUCKET` | `sage-snapshots` | Snapshot bucket |
| `MINIO_SECURE` | `false` | TLS to MinIO |

## Refresh cadence (not env vars)

`refresh_after_days` (default **30**) controls when a ready version counts as
stale for the `refresh_stale` sweep. It is a per-library catalog attribute,
not an environment variable in the reviewed sources. See
[sage-ingestion-freshness.html](sage-ingestion-freshness.html).

## Uncertain / not found

- The derived `EMBEDDING_*` names above follow the `env_prefix="EMBEDDING_"`
  convention in `services/embedding/settings.py`; the file was reviewed and
  the listed fields match it.
- No scheduler-related variables exist in the reviewed sources — the weekly
  refresh and monthly DevDocs sync have no scheduling configuration yet.
- No ingestion tunables (chunk target/overlap, politeness rate) are exposed
  as env vars in the reviewed sources; they are code constants
  (420/48 chunking, 1 req/s per host, 5000-URL sitemap cap).
