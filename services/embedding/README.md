# Embedding service — EmbeddingGemma 2 (spec §8)

FastAPI contract adapter with local CPU (`st`) and shared GPU (`vllm`)
backends. For H200 GPU 0 use the [shared deployment guide](../../deploy/shared-embedding.md)
and Compose overlay; both apps share one EmbeddingGemma 2 model instance.
The original CPU deployment remains available.

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
| `EMBEDDING_BACKEND` | `st` | `st` = local CPU; `vllm` = shared GPU; `mock` = tests/dev |
| `EMBEDDING_BATCH_SIZE` | `32` | |
| `EMBEDDING_MAX_CONCURRENCY` | `4` | semaphore around the encode lock |
| `EMBEDDING_MAX_INPUT_CHARS` | `32768` | hard reject above this — inputs arrive pre-chunked (§7) |
| `EMBEDDING_PORT` | `8003` | |

Startup **fails fast** when the artifact is missing/invalid — production
services never download at runtime (§3, `HF_HUB_OFFLINE=1`).

## Matryoshka

Coded for 768 dims. A validated width change (512/256/128) plugs into
`EMBEDDING_DIMENSION` with re-normalization; the service verifies the first
vector's width and normalization and fails closed on mismatch. Any width
change requires full re-embedding per the migration rule — the service will
not silently mix widths.

## Docker

The image copies the sibling contract files
(`services/ingestion/{embeddings,config,chunker,tree,identifiers}.py`)
**without** `services/ingestion/__init__.py` (which would import pdftext and
other heavy deps). `reuse.py` loads them without executing the package init.

## Shared GPU backend settings

| Variable | Default | Meaning |
|---|---|---|
| `EMBEDDING_VLLM_BASE_URL` | `http://vllm-embedding:8000/v1` | Shared OpenAI-compatible embedding API |
| `EMBEDDING_VLLM_API_KEY` | unset | Shared service authentication secret |
| `EMBEDDING_VLLM_TIMEOUT_SECONDS` | `120` | Bounded request timeout |

Set `EMBEDDING_BACKEND=vllm` through the deployment overlay. The adapter
preserves task formatting, uses pooled HTTP connections, validates every
vector, and supports concurrent requests without the local model encode lock.
The shared server supplies native 768-wide vectors; optional Matryoshka
truncation is followed by normalization. Width changes require re-embedding.
The health `device` value is `cuda:0 (remote vLLM)` in this mode; no local
checkpoint is needed. Upstream errors fail closed; there is no silent CPU or
mock fallback. `Dockerfile.vllm` installs only HTTP/API/image dependencies.
