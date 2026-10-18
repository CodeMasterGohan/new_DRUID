# Experimental Rust CPU fast path — New_DRUID

## Decision: leave disabled

New_DRUID uses Python orchestration and native optimized libraries for
embedding, MiniLM cross-encoder reranking, Milvus, and PDF processing.
Reimplementing model inference in Rust would not change CPU/GPU kernels
without replacing the ML inference engine.

An optional PyO3 module now implements **reciprocal rank fusion (RRF)**
and **batched passage truncation** in Rust. It lives under
`rust/retrieval_fastpath/`, with Python implementations retained in
`services/retrieval/fusion.py` and `services/retrieval/reranker.py`.

**Do not enable `DRUID_RUST_FASTPATH=1` by default.** Initial benchmark
on GitHub Actions CPython 3.11, Linux x86_64 (Run
[39092642545](https://github.com/CodeMasterGohan/new_DRUID/actions/runs/39092642545)):

| Microbenchmark | Python (median) | Rust (median) | Interpretation |
| --- | ---: | ---: | --- |
| RRF, 10,000 ranked candidates | 7.791 ms | 8.504 ms | Rust about 9% slower |
| 1,000 long passage truncations | 0.798 ms | 8.963 ms | Rust much slower |

The copies/allocations across the Python ↔ Rust boundary outweighed the
small Python loops. These measurements cover **only CPU post-processing**
and must **not** be described as embedding, MiniLM model inference, RAG
latency, or H200 throughput improvements.

## Build and verify (developer only)

Use the exact interpreter and architecture of the service image.
A compatible installed Rust compiler, Python 3.11, and maturin are needed:

```bash
python3.11 -m pip install 'maturin>=1.7,<2' pytest
maturin build --release --interpreter python3.11 \
  --manifest-path rust/retrieval_fastpath/Cargo.toml --out dist/rust
python3.11 -m pip install dist/rust/druid_retrieval_fastpath-*.whl
python3.11 -m pytest -q -s tests/test_rust_retrieval_fastpath.py
```

The optional wheel is **not** baked into regular production Docker images.
Only after explicitly installing the wheel inside the running image and
passing parity tests can `DRUID_RUST_FASTPATH=1` be used in an isolated
benchmark environment. Missing modules fail loudly if opted in. Ordinary
deployment has no new Rust runtime requirement.

Benchmarks vary with corpus size, interpreter, CPUs and input shape.
Rerun with realistic candidate counts (current production defaults are
only ~50 dense + 50 BM25) and measure end-to-end p50/p95, CPU, RSS and
multi-user throughput, not only this microbenchmark. Do not enable a
compiled dependency without an actual positive performance signal.

## Correct optimization priorities

1. Profile `HttpEmbeddingClient.embed_query`, Milvus branches and
   `MiniLMReranker.score` separately using representative queries.
2. Batch embedding and reranking where possible; measure Torch CPU thread
   contention under concurrency. Keep pre-staged model weights offline.
3. If CPU model inference is saturated, consider an optimized inference
   runtime such as ONNX Runtime/OpenVINO, benchmark it against current
   accuracy and CPU latency, and avoid changes to tenant filtering.
4. Preserve HMAC/Keycloak/DB group authorization outside any optional
   Rust extension. The extension only sees already-filtered candidates.
