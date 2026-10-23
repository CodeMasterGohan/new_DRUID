# CPU and GPU processing modes

Set `DRUID_PROCESSING_MODE=cpu` to exercise native PDF ingestion without a GPU
OCR service. The default remains `gpu`, preserving existing deployments.
Embeddings and reranking run on CPU in both modes; `gpu` enables the existing
external olmOCR service for pages that require visual parsing.

For an already configured deployment, restart the worker with the selected mode:

```bash
DRUID_PROCESSING_MODE=cpu docker compose --env-file .env -f deploy/docker-compose.yml up -d ingestion
```

Use `DRUID_PROCESSING_MODE=gpu` to restore the GPU OCR path, and start the
separate GPU Compose project on a compatible host with staged OCR artifacts.
Changing the flag does not start or stop that GPU project automatically.

CPU mode still requires the normal catalog, storage, index, tokenizer and
embedding artifacts. It does not replace production inference with fake vectors.
It processes supported native text pages. Pages requiring visual OCR are rendered
on CPU and quarantined with an explicit `deferred` status and explanation. The
whole version enters `REVIEW_REQUIRED`; partial content is never marked READY or
indexed. Switching back to GPU does not automatically retry these terminal jobs.
Review decisions alone do not invoke OCR or reprocess a document.

The pinned `pdftext==0.4.1` has no `table_output` API. Native text extraction
remains available; detected table pages use the visual path (or defer in CPU
mode), rather than failing every PDF import on an unavailable API.

Run the CPU regression tests without model weights:

```bash
python -m pip install -r ci/requirements-test.txt pdftext==0.4.1 pypdfium2==4.30.0 pillow==13.0.0
python -m pytest -q services/ingestion/tests/test_cpu_mode.py services/ingestion/tests/test_pipeline.py
```

These tests extract real generated PDF files, render deferred pages, and verify
that CPU processing never calls OCR or indexes deferred content. The GPU routing
test uses an OCR fixture; it does not validate GPU inference. Optional native
tests skip if PDF dependencies are absent, so inspect the test counts.

Pretrained embedding/reranking checks additionally require staged, verified
artifacts. The onboarding environment currently cannot reach Hugging Face under
its network policy; a CPU/GPU flag does not resolve that separate prerequisite.
