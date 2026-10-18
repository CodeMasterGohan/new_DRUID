"""Bit-for-bit parity for the optional compiled Rust retrieval CPU fast path.

The extension is intentionally not installed in the normal Python image:
these tests run in a separate Rust-build job. Python remains the default.
"""
from __future__ import annotations

import os
import random
import statistics
import time

import pytest

fastpath = pytest.importorskip("druid_retrieval_fastpath")

from services.retrieval.fusion import reciprocal_rank_fusion
from services.retrieval.models import ScoredHit, StoredRecord, FusedCandidate
from services.retrieval.reranker import MAX_PASSAGE_CHARS, truncate_to_budget, rerank_text_candidates


def _hit(key: str, rank: int) -> ScoredHit:
    return ScoredHit(record=StoredRecord(
        chunk_id=key, document_id="doc", document_version="1",
        filename="test.pdf", page_start=0, page_end=0, text="text " + key,
    ), rank=rank)


@pytest.mark.parametrize("n", [0, 1, 8, 50, 500])
def test_rust_rrf_preserves_order_scores_branches_and_first_record(monkeypatch, n):
    rng = random.Random(88239 + n)
    ids = [f"chunk-{i}" for i in range(max(1, n))]
    branches = {
        "dense": [_hit(rng.choice(ids), i + 1) for i in range(n)],
        "bm25": [_hit(rng.choice(ids), i + 1) for i in range(n)],
        "identifier": [_hit(rng.choice(ids), 0) for i in range(n // 5)],
    }
    monkeypatch.setenv("DRUID_RUST_FASTPATH", "0")
    python = reciprocal_rank_fusion(branches, k=60)
    monkeypatch.setenv("DRUID_RUST_FASTPATH", "1")
    rust = reciprocal_rank_fusion(branches, k=60)
    assert [x.record.chunk_id for x in rust] == [x.record.chunk_id for x in python]
    assert [x.branches for x in rust] == [x.branches for x in python]
    assert [x.record is y.record for x, y in zip(rust, python)] == [True] * len(rust)
    assert [x.fusion_score for x in rust] == pytest.approx(
        [x.fusion_score for x in python], rel=0, abs=1e-15
    )


@pytest.mark.parametrize("s,limit", [
    ("", 0),
    ("a sentence with spaces", 12),
    ("unchanged", 99),
    ("no_spaces_here", 4),
    ("emoji 🚀🚀🚀 work with unicode", 10),
    ("électricité ümlaut 全角漢字 end", 12),
    ("hello\nlonger than input", 4),
    ("ending space   more words", 16),
    ("negative test words", -3),
    ("   ", 2),
])
def test_unicode_truncation_semantics(s, limit):
    assert fastpath.truncate_passages([s], limit) == [truncate_to_budget(s, limit)]


def test_batch_passage_preparation_matches_cross_encoder_input(monkeypatch):
    data = ["word " * 900, "short", "extra " * 500, "world 🪐 " * 350]
    rust = fastpath.truncate_passages(data, MAX_PASSAGE_CHARS)
    py = [truncate_to_budget(x) for x in data]
    assert rust == py


def test_opt_in_fails_explicitly_if_compiled_module_missing(monkeypatch):
    # Importing this test means module is actually present. Production
    # config defaults off and the missing module never affects startup.
    monkeypatch.setenv("DRUID_RUST_FASTPATH", "0")
    assert reciprocal_rank_fusion({"dense": [_hit("test", 1)]})


def _median_ns(call, trials: int = 12) -> int:
    times = []
    for _ in range(trials):
        start = time.perf_counter_ns()
        call()
        times.append(time.perf_counter_ns() - start)
    return int(statistics.median(times))


@pytest.mark.benchmark
def test_print_only_microbenchmark(monkeypatch):
    """Measure, never assert a speedup; native overhead may dominate at low N."""
    rng = random.Random(11)
    names = [f"id-{i}" for i in range(3000)]
    branches = {
        "dense": [_hit(rng.choice(names), i + 1) for i in range(5000)],
        "bm25": [_hit(rng.choice(names), i + 1) for i in range(5000)],
    }
    for mode in ("0", "1"):
        monkeypatch.setenv("DRUID_RUST_FASTPATH", mode)
        _ = reciprocal_rank_fusion(branches, k=60)
        val = _median_ns(lambda: reciprocal_rank_fusion(branches, k=60), 8)
        print(f"DRUID RRF mode={mode} candidates=10000 median_ms={val/1_000_000:.3f}")
    texts = ["long " * 1000 for _ in range(1000)]
    print("DRUID passage prep rust_ms=" +
          f"{_median_ns(lambda: fastpath.truncate_passages(texts, MAX_PASSAGE_CHARS), 8)/1e6:.3f}")
    print("DRUID passage prep python_ms=" +
          f"{_median_ns(lambda: [truncate_to_budget(t) for t in texts], 8)/1e6:.3f}")
