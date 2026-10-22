# Shared EmbeddingGemma 2 on H200 GPU 0

This opt-in deployment replaces CPU embedding inference for both DRUID and
SAGE with **one** native vLLM pooling server. The existing application APIs
and task prefixes stay intact. DRUID ingestion sends already formatted
chunks and page images directly to the same server; SAGE ingestion continues
through its existing embedding API. Reranking, PDF parsing and OCR are separate
workloads; this change moves embedding inference to the GPU.

This mode supersedes the original CPU-only embedding/GPU-exclusivity design
when the shared-embedding overlay is selected. The original Compose files
still support explicit local CPU deployments.

## Specialized configuration

| Setting | Default | Reason |
|---|---|---|
| Image | `vllm/vllm-openai:v0.32.0` | Pinned release with native EmbeddingGemma 2 support |
| Model | Local `google/embeddinggemma-2` snapshot | Offline serving, original learned projection and mean pooling |
| GPU | Physical device `0`, TP=1 | One small model; avoid inter-GPU communication |
| Precision | BF16 | Model-safe on H200; FP16 can produce NaNs |
| Modalities | One image per request; audio/video disabled | Both text/code and DRUID page images; unused audio tower skipped |
| Output | 768 dimensions | Preserve existing schema and embedding contract |
| Context | 8,192 tokens | Native model limit; overlong inputs fail instead of silent truncation |
| Active sequences | 64 | Starting concurrency for mixed query/ingestion traffic |
| Tokens per batch | 32,768 | Accommodate many short chunks with bounded long-input batches |
| GPU memory utilization | 0.10 | Conservative starting budget when GPU 0 also serves OCR |
| Prefix cache/chunked prefill | Disabled | Bidirectional whole-input pooling, not autoregressive decoding |
| Execution | Eager | Native model lacks torch.compile integration in this pinned release |
| Application adapters | Batch 64, concurrency 8 | Permit concurrent HTTP requests and server-side scheduling |

These are workload-specific starting settings, **not measured H200 optima**.
`gpu-memory-utilization` is a vLLM allocation/planning setting, not a hard GPU
memory quota or compute reservation. Leave room for activations and vision
workspaces. If olmOCR or another vLLM process shares GPU 0, start both and
stress mixed traffic before raising either budget. The existing OCR Compose
uses a 0.7 budget and does not coordinate scheduling with this new server.

Do not add generation-oriented KV offload, speculative decoding, FP8/INT4,
or tensor parallelism to this small embedding workload without measurement.
The model stays at its original precision and pooling/projection defaults.

## Start the shared server once

Run on the H200 Docker host from the **new_DRUID repository root**. NVIDIA
Container Toolkit and a driver compatible with the pinned vLLM image must
already work. Pre-stage the complete model snapshot and container image for
offline deployment; the service never downloads model weights at startup.

```bash
cp deploy/shared-embedding/.env.example deploy/shared-embedding/.env
# Edit that file: set EMBEDDING_MODEL_DIR and a strong EMBEDDING_VLLM_API_KEY.
# For example, generate a secret with: openssl rand -hex 32
docker network inspect shared-embedding-net >/dev/null 2>&1 || docker network create shared-embedding-net
docker compose --env-file deploy/shared-embedding/.env -f deploy/shared-embedding/compose.yml config --quiet
docker compose --env-file deploy/shared-embedding/.env -f deploy/shared-embedding/compose.yml up -d --wait
```

No host port is published. Both application adapters reach
`http://vllm-embedding:8000/v1` on `shared-embedding-net`; keep both stacks on
this Docker host. For a separate host, add an authenticated TLS reverse proxy
and change the overlay URLs; Docker network names do not span hosts.

## Check compatibility before cutover

Keep the same staged checkpoint, task prefixes, width and image processing.
CPU-to-BF16/vLLM numerics can still differ. From DRUID root, build the existing
CPU image and compare it against the new server using a representative page
image. It needs the same key used by the shared server:

```bash
docker build -f services/embedding/Dockerfile -t druid-embedding-cpu .
export EMBEDDING_VLLM_API_KEY='the-same-secret'
docker run --rm --network shared-embedding-net \
  -e EMBEDDING_VLLM_API_KEY -e PYTHONPATH=/app \
  -v /DATA/docker/mounts/models/google/embeddinggemma-2:/model:ro \
  -v "$PWD/deploy/shared-embedding/verify.py:/verify.py:ro" \
  -v /absolute/path/to/representative-page.png:/page.png:ro \
  druid-embedding-cpu python /verify.py --model-path /model --image /page.png
```

The script checks multiple query tasks, code/documents, a long input, and an
image against CPU vectors (cosine >=0.999). Repeat with real domain examples
and validate top-k search results in **both applications** before retaining
existing indexes. If parity fails, leave CPU serving active and investigate;
if intentionally changing the embedding space, fully re-embed into a new
index/version rather than mixing vectors. SAGE retains its own vendored task
contract without rewriting it.

## Enable the application overlays

Put the **same** `EMBEDDING_VLLM_API_KEY` in DRUID's root `.env` and SAGE's
`deploy/.env`. Do not commit secrets. Start the shared server first.

From DRUID root:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml -f deploy/docker-compose.shared-embedding.yml up -d --build embedding ingestion
```

From SAGE root:

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml -f deploy/docker-compose.shared-embedding.yml up -d --build embedding
```

Include the overlay in **every subsequent deployment command**, including CI
or Dockge stack definitions. Starting the base file alone restores CPU mode.
The existing model/tokenizer mounts remain available for other components.
Only the adapters join the shared network; DRUID ingestion joins it as well
for its direct embedding calls. Database and gateway networks stay isolated.

Adapters use a lightweight `Dockerfile.vllm` with no Torch/Transformers/model
weights. The default CPU Dockerfile remains available for compatibility checks
and rollback. Neither adapter requests a GPU; only the shared server does.
Health checks probe the upstream engine, startup performs a real embedding,
HTTP failures do not fall back to CPU/mock, and vector count/order/width,
finite values and normalization are checked for every response.

## Tune with the actual workload

Monitor `nvidia-smi` on GPU 0, vLLM logs, and `/metrics` on the internal service.
Warm up first, then measure both apps together: queries during document
ingestion, representative page-image requests, and simultaneous OCR. Record
vectors/sec, request p50/p95, errors and peak GPU memory. Increase sequences
64 -> 128 and batch tokens 32768 -> 65536 independently only if measurements
improve throughput without unacceptable query latency or memory pressure.
The memory budget and both batching knobs are exposed in the shared `.env`.

Rollback by running the original application Compose files with
`up -d --build --force-recreate embedding` (also `ingestion` for DRUID). Stop
the shared server only after both applications have switched back. No
automatic index deletion or re-embedding is performed by this deployment.

## Primary references

- [vLLM 0.32.0 release](https://github.com/vllm-project/vllm/releases/tag/v0.32.0)
- [EmbeddingGemma 2 model card, precision and prefixes](https://huggingface.co/google/embeddinggemma-2)
- [vLLM native model and pooling implementation](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/model_executor/models/embedding_gemma2.py)
- [vLLM upstream parity tests](https://github.com/vllm-project/vllm/blob/v0.32.0/tests/models/multimodal/pooling/test_embedding_gemma2.py)
