# Embedding service — EmbeddingGemma 2 (spec §8)

A lightweight FastAPI adapter for the shared Docker vLLM pooling server used
by DRUID and SAGE. Default `EMBEDDING_BACKEND=vllm` serves the existing
`google/embeddinggemma-2` checkpoint. This adapter applies task prefixes and
preserves DRUID's API; the shared server owns model weights and GPU inference.
DRUID ingestion uses the same remote backend for chunk and page-image vectors.
See [shared deployment](../../deploy/shared-embedding.md) for setup and smoke checks.

## The §8 contract (read this before touching prefixes)

The task-aware asymmetric prefix pairs are **contractual**. The wrong prefix
is a quality bug, not a cosmetic choice. The contract lives in exactly one
place — `services/ingestion/embeddings.py` (`PrefixContract`,
`build_query_text`, `build_document_text`) — and this service reuses it via
`reuse.py`. It does not duplicate the prefix strings; a test asserts
byte-equality with the sibling's formatters.

| Kind | Task | Formatted input |
|---|---|---|
| document | `question_answering` (default) | `title: {heading_path} \| text: {chunk}` |
| document | `code_retrieval` | `title: {heading_path} \| text: {code}` (code path) |
| query | `question_answering` (default) | `task: question answering \| query: {q}` |
| query | `code_retrieval` | `task: code retrieval \| query: {q}` |
| query | `search_result` (approved alt) | `task: search result \| query: {q}` |

## Endpoints

- `POST /embed` — `{"documents": [{"text", "title", "is_code"}], "queries": [...], "task": "question_answering"|"code_retrieval"|"search_result", "modality": "text"}` → 768-dim normalized vectors. Prefixes are applied server-side.
- `POST /embed_image` — `{"image_b64": "<png>", "page_label": "Page 3 of doc.pdf"}` → vector in the **same** unified space + `metadata: {"modality": "page_image"}`. `text` holds the page label, never OCR output. (`object_key` → 501: RustFS fetch is deploy-time work.)
- `GET /health` — model, modality config, dimension, backend, `model_loaded`.

## Configuration (env)

| Var | Default | Notes |
|---|---|---|
| `EMBEDDING_MODEL_PATH` | (required for `st`) | Pre-staged artifact dir, read-only mount. `EMBEDDING_MODEL_DIR` accepted as alias. |
| `EMBEDDING_MODALITY_CONFIG` | `text+image` | or `text-only` (fallback) |
| `EMBEDDING_DIMENSION` | `768` | Must be a validated Matryoshka width (768/512/256/128) |
| `EMBEDDING_BACKEND` | `vllm` | `st` = optional local CPU backend; `mock` = tests/dev |
| `EMBEDDING_MODEL_ID` | `google/embeddinggemma-2` | Must match vLLM's served model name |
| `EMBEDDING_VLLM_URL` | `http://vllm-embedding:8000/v1` | OpenAI base URL including `/v1` |
| `EMBEDDING_VLLM_API_KEY` | empty | Optional shared server key |
| `EMBEDDING_VLLM_TIMEOUT_S` | `120` | Positive finite request timeout |
| `EMBEDDING_BATCH_SIZE` | `32` | |
| `EMBEDDING_MAX_CONCURRENCY` | `4` | semaphore around the encode lock |
| `EMBEDDING_MAX_INPUT_CHARS` | `32768` | hard reject above this — inputs arrive pre-chunked (§7) |
| `EMBEDDING_PORT` | `8003` | |

Startup makes a real embedding request and **fails** if the shared server is
unavailable or returns invalid vectors. There is no fallback to local inference.
For `st`, startup fails when the pre-staged artifact is missing/invalid.

## Matryoshka

Coded for 768 dims. A validated width change (512/256/128) plugs into
`EMBEDDING_DIMENSION` with re-normalization; the service verifies the first
vector's width and normalization and fails closed on mismatch. The vLLM client validates every returned row, requests native 768-wide vectors without the unsupported `dimensions` API option, then truncates and re-normalizes locally. Any width
change requires full re-embedding per the migration rule — the service will
not silently mix widths.

## Docker

The image copies the sibling contract files
(`services/ingestion/{embeddings,config,chunker,tree,identifiers}.py`)
**without** `services/ingestion/__init__.py` (which would import pdftext and
other heavy deps). `reuse.py` loads them without executing the package init.


The default image has no torch or sentence-transformers. To run the optional
local backend, rebuild with `--build-arg INSTALL_LOCAL_EMBEDDINGS=true`, set
`EMBEDDING_BACKEND=st`, and mount the artifact read-only at
`EMBEDDING_MODEL_PATH`. `mock` is for tests only.
