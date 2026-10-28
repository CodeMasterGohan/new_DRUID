# vLLM serving config — olmOCR-2-7B-FP8 on the single H200

OCR and the shared EmbedGemma vLLM server are separate GPU engines.
The MiniLM reranker remains on CPU. See [shared-embedding.md](shared-embedding.md)
for the embedding server shared by DRUID and SAGE and its deployment lifecycle.
Measure their concurrent peak usage before production rollout.

## Checkpoint

```text
allenai/olmOCR-2-7B-1025-FP8
```

FP8 is the approved **production** checkpoint (Ai2: "we recommend using the
FP8 version for all practical purposes except further fine tuning"; bench
82.4 vs 82.3 overall — no meaningful quality loss). The BF16 checkpoint
(`allenai/olmOCR-2-7B-1025`) is approved for fine-tuning only.

## Launch

See `docker-compose.gpu.yml`. Equivalent bare command:

```bash
vllm serve /models/allenai/olmOCR-2-7B-1025-FP8 \
  --served-model-name olmocr-2-fp8 \
  --host 0.0.0.0 --port 8000 \
  --gpu-memory-utilization 0.7 \
  --max-model-len 8192 \
  --limit-mm-per-prompt image=4
```

Environment: `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`. The checkpoint is
mounted read-only from the host artifact registry; a missing checkpoint must
fail startup, never trigger a download.

## Memory math (H200 141 GB HBM3e)

| Component | Size |
|---|---|
| FP8 weights (7B params × 1 byte) | ~7 GB |
| vLLM reservation (`0.7 × 141 GB`) | ~98.7 GB total for weights + KV cache + activations |
| Shared embedding reservation (`0.15 × 141 GB`) | ~21.2 GB |
| Remaining GPU headroom | ~21 GB |

olmOCR renders each page at longest-dimension 1288 px and generates
markdown-ish output; per-request KV cache is the dominant variable cost.
`--gpu-memory-utilization 0.7` is the **initial** cap — tune against KV-cache
pressure under concurrent page renders (spec §13: measure olmOCR throughput
with embedding/query services under load).

## Request contract

OpenAI-compatible `/v1/chat/completions` with a vision message part (base64
PNG, longest dim 1288 px). The ingestion service must use the olmOCR
toolkit's prompt builders (`build_no_anchoring_v4_yaml_prompt`) — never
hand-rolled prompts — to preserve the checkpoint's expected preprocessing.

## Health & smoke test

```bash
curl -f http://vllm-olmocr:8000/health
curl http://vllm-olmocr:8000/v1/models   # expect served-model-name olmocr-2-fp8
```

## Tuning guidance

- Increase `--max-num-seqs` only after measuring p99 page latency; ingestion
  is throughput-bound, queries don't touch this service.
- If OOM under burst: lower `--gpu-memory-utilization` is wrong direction —
  instead cap ingestion worker concurrency (the gateway quota
  `QUOTA_INGEST_PAGES_PER_DAY` and worker count are the levers).
- FP8 on Hopper (sm_90a) requires a vLLM build with FP8 kernels; verify the
  image tag supports it before deploy (see TODO in docker-compose.gpu.yml).
