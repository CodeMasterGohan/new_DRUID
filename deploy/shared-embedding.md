# Shared EmbedGemma vLLM service

DRUID and SAGE default to `google/embeddinggemma-2` on one shared Docker
vLLM server. Their `embedding` services keep their existing HTTP contracts
and apply the same document/query prefixes, but perform inference over
`http://vllm-embedding:8000/v1`. DRUID ingestion sends its text and page-image
requests to the same server. The default adapters do not install or load
local model weights. Inference failures stop startup or fail the request;
there is no automatic local or mock fallback.

The two repositories contain **identical** `docker-compose.embedding.yml`
files with the fixed project name `shared-embedding`. Start this project
once, from either checkout. Do not combine its file with an application's
Compose file or override its project name with `-p` / `COMPOSE_PROJECT_NAME`.
Both applications join the external `embedding-net` network; the shared
server publishes no host ports. Stopping either application stack leaves
the shared server running.

## Before starting

- Install Docker Engine, Compose v2 and NVIDIA Container Toolkit on the GPU
  host. The default GPU selector is `EMBEDDING_VLLM_GPU_ID=0`.
- Stage the complete `google/embeddinggemma-2` checkpoint, tokenizer,
  processor and `chat_template.jinja` under
  `/DATA/docker/mounts/models/google/embeddinggemma-2`. The registry root
  can be changed with `MODEL_DIR`. Check the existing artifact manifest
  before deploying; weights are mounted read-only and downloads are disabled.
- The pinned `vllm/vllm-openai:v0.32.0` image provides native
  `EmbeddingGemma2Model` text and image pooling. An older OCR vLLM image
  cannot substitute for this image. The explicit model chat template is
  needed for image messages; audio and video towers are disabled.
- If colocating with DRUID OCR, budget both GPU engines together. The
  embedding memory fraction defaults to `0.15`, alongside OCR's `0.70`.
  These settings leave headroom but are starting values, not a measured
  capacity guarantee. Measure concurrent OCR, page images, ingestion and
  query load before rollout. Use a different GPU ID when available.

## Start once, then start the applications

From the chosen repository root, choose its configured environment file:

```bash
# DRUID:
EMBEDDING_ENV_FILE=.env
# SAGE instead:
# EMBEDDING_ENV_FILE=deploy/.env

docker network inspect embedding-net >/dev/null 2>&1 || docker network create embedding-net
docker compose --env-file "$EMBEDDING_ENV_FILE" -f deploy/docker-compose.embedding.yml config --quiet
docker compose --env-file "$EMBEDDING_ENV_FILE" -f deploy/docker-compose.embedding.yml up -d --wait --wait-timeout 600
```

The application environment examples default to:

```dotenv
EMBEDDING_BACKEND=vllm
EMBEDDING_MODEL_ID=google/embeddinggemma-2
EMBEDDING_DIMENSION=768
EMBEDDING_VLLM_URL=http://vllm-embedding:8000/v1
EMBEDDING_VLLM_TIMEOUT_S=120
EMBEDDING_VLLM_API_KEY=
```

When upgrading an existing installation, update its untracked environment
file: change `EMBEDDING_BACKEND=st` to `vllm` and set the shared URL before
recreating adapters/workers. Use matching model, URL and dimension settings
in both installations. If setting an API key, configure the same
`EMBEDDING_VLLM_API_KEY` for the shared server and every client; changing
one application environment file does not update the other project.

Then use the application's normal Compose commands, with its environment
file explicitly supplied. For example, from DRUID:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d --build
```

From SAGE:

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build
```

The shared project has an independent lifecycle. Application adapters
warm up against vLLM and refuse to start if the service is unavailable or
returns invalid vectors. Bring the shared server up first; after a server
outage, restart an adapter that exhausted its startup attempts.

## Verify text and image inference

Run the supplied smoke test on `embedding-net`, without exposing a host
port. This sends both a prefixed text and a PNG data URL, checks model
identity, 768 dimensions, finite values and normalization. Pull or stage
the Python image before restricting external network access.

```bash
docker run --rm --network embedding-net \
  -e EMBEDDING_VLLM_API_KEY \
  -v "$PWD/deploy/check_embedding.py:/check_embedding.py:ro" \
  python:3.12-slim python /check_embedding.py http://vllm-embedding:8000/v1
```

If authentication is enabled, export the same key into this shell without
printing it before running the command. Confirm the application embedding
`/health` reports backend `vllm`, and ingest/search a representative document
in each application. This smoke test checks serving compatibility; assess
retrieval quality using a representative corpus. The default model, task
prefixes and 768-dimension contract remain unchanged. Switching models,
pooling, prefixes or vector widths requires re-embedding the corpus.

## Explicit local development option

`st` and `mock` remain opt-in. For local sentence-transformers, build the
adapter with `INSTALL_LOCAL_EMBEDDINGS=true` and supply its model mount and
`EMBEDDING_BACKEND=st` through an operator Compose override; the default
adapter image omits these heavy dependencies. For example:

```yaml
services:
  embedding:
    build:
      args:
        INSTALL_LOCAL_EMBEDDINGS: "true"
    environment:
      EMBEDDING_BACKEND: st
      EMBEDDING_MODEL_PATH: /models/google/embeddinggemma-2
    volumes:
      - /DATA/docker/mounts/models:/models:ro
```

For DRUID's local ingestion backend, also set `EMBEDDING_BACKEND=st` and
`EMBEDDING_MODEL_PATH=/models/google/embeddinggemma-2` on its `ingestion`
service. Local model dependencies must be installed there as well. This is
an explicit alternative; the deployed default remains the shared vLLM
service. `mock` produces deterministic fake vectors and is only for tests.
