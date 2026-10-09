# Ingestion workers (`services/ingestion`)

Implements spec §5 (ingestion + page router), §6 (canonical tree), §7
(chunking), §8 (EmbeddingGemma 2 embeddings), §12 (job state machine).

## Layout

| Module | Responsibility |
|---|---|
| `config.py` | All thresholds/config. Router thresholds are named + PROVISIONAL (M1 calibrates them); no magic numbers in logic. |
| `router.py` | Page-level routing: `NATIVE` / `VISUAL` / `SKIP_BLANK` on computed signals (density, replacement chars, private-use glyphs, order disagreement, columns, table signals). Pure functions. |
| `tables.py` | Table-region detection from pdftext line geometry (row pitch, columnar gutters, grid alignment) with confidence scoring. High-confidence → `table_output`; low → page to olmOCR. |
| `native_worker.py` | Process-based pdftext extraction. **PDFium is not thread-safe — processes only, never threads.** Documents the per-worker page threshold. |
| `olmocr_client.py` | HTTP client for vLLM-served `allenai/olmOCR-2-7B-1025-FP8`. Prompts come from the olmOCR toolkit builders — never hand-rolled. Retry → review queue. |
| `tree.py` | Canonical typed document tree (Document→Page→Heading/Paragraph/List/Table/CodeBlock/Figure), stable content-derived node IDs, markdown derivation, diagnostics. |
| `chunker.py` | Structural chunker: 420/48 targets, tables split by row groups with repeated headers, **hard rule** — no chunk exceeds `512 − max_query_tokens` tokens (split, never truncate; violations raise). |
| `identifiers.py` | Configurable exact-match identifier extraction for the Milvus `identifiers` field. |
| `embeddings.py` | EmbeddingGemma 2 backend abstraction. Contractual task-aware prefixes (`title: … | text: …` / `task: question answering | query: …`, code pair for code). Page-image records in the unified 768-dim space. `MockEmbeddingBackend` for tests/dev. |
| `pipeline.py` | RQ worker entry points. `run_ingestion_job` drives UPLOADED→…→READY (+RETRY_PENDING/FAILED/REVIEW_REQUIRED) against the real catalog models; `run_deletion_job` drives the §12 deletion workflow. Seams: `IndexSink` (Milvus agent), `ObjectStore` (RustFS prod / memory for tests). |

## Running

```bash
# Worker (Docker CMD): rq worker --url "$REDIS_URL" ingest delete
# Enqueue from the document API:
from services.ingestion.pipeline import enqueue_ingestion_job
```

Env: `DATABASE_URL`, `REDIS_URL`, `OLMOCR_VLLM_URL`, `EMBEDDING_MODEL_PATH`
(pre-staged, fail-closed), `MINILM_TOKENIZER_PATH` (pre-staged, fail-closed),
`PDFTEXT_WORKERS`, `ROUTER_*` threshold overrides, `CHUNK_TARGET_TOKENS`.

## Offline policy

No module downloads models, tokenizers, or weights. Backends raise explicit
errors when pre-staged artifacts are missing (`HF_HUB_OFFLINE=1` /
`TRANSFORMERS_OFFLINE=1` are set in the Dockerfile; they supplement, not
replace, this fail-closed behavior).

## Tests

```bash
python3 -m pytest services/ingestion/tests/ -q   # 51 tests
```

Pure-logic tests inject a word-count tokenizer; production wires the
pre-staged MiniLM tokenizer. pdftext/vLLM are never touched (lazy imports;
pipeline stages monkeypatched).
