"""Production ingestion uses shared GPU inference without loading a CPU model."""
import pytest

from services.embedding.vllm_backend import VllmEmbeddingBackend
from services.ingestion import milvus_sink, pipeline


@pytest.fixture
def production_env(monkeypatch):
    for name in ("MINILM_TOKENIZER_PATH", "RUSTFS_ENDPOINT", "RUSTFS_ACCESS_KEY",
                 "RUSTFS_SECRET_KEY", "RUSTFS_BUCKET", "MILVUS_URI"):
        monkeypatch.setenv(name, "test-only")
    monkeypatch.delenv("EMBEDDING_MODEL_PATH", raising=False)
    monkeypatch.setattr(milvus_sink, "MilvusIndexSink", lambda *a: pipeline.LoggingIndexSink())
    monkeypatch.setattr(pipeline, "RustFSObjectStore", lambda *a: pipeline.MemoryObjectStore())


def test_gpu_ingestion_needs_no_local_embedding_checkpoint(monkeypatch, production_env):
    monkeypatch.setenv("EMBEDDING_BACKEND", "vllm")
    deps = pipeline.build_production_deps()
    try:
        assert isinstance(deps.embedding_backend, VllmEmbeddingBackend)
        assert deps.embedding_backend.settings.dimension == 768
    finally:
        deps.embedding_backend.close()


def test_cpu_ingestion_still_requires_staged_model(monkeypatch, production_env):
    monkeypatch.setenv("EMBEDDING_BACKEND", "st")
    with pytest.raises(RuntimeError, match="EMBEDDING_MODEL_PATH"):
        pipeline.build_production_deps()


def test_mock_ingestion_remains_forbidden(monkeypatch, production_env):
    monkeypatch.setenv("EMBEDDING_BACKEND", "mock")
    with pytest.raises(RuntimeError, match="mock embeddings are forbidden"):
        pipeline.build_production_deps()
