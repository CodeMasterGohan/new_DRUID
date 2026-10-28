"""Production ingestion must encode chunks and images on the shared server."""

import json
from unittest.mock import Mock

import httpx
import pytest

from services.ingestion import milvus_sink, pipeline
from services.ingestion.chunker import Chunk
from services.ingestion.vllm_backend import VllmEmbeddingBackend


def test_remote_ingestion_formats_chunks_queries_and_page_images():
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "google/embeddinggemma-2",
            "data": [{"index": 0, "embedding": [3.0, 4.0] + [0.0] * 766}],
        })

    backend = VllmEmbeddingBackend(transport=httpx.MockTransport(handle))
    chunk = Chunk(chunk_id="c", document_id="d", document_version="1", parent_id=None,
                  page_start=0, page_end=0, chunk_order=0, heading_path=["Intro"],
                  text="A document", identifiers=[], metadata={})
    assert backend.embed_chunk(chunk)[:2] == [0.6, 0.8]
    assert backend.embed_query("How?", task="code")[:2] == [0.6, 0.8]
    assert backend.embed_images([b"\x89PNG\r\n\x1a\nrender"])[0][:2] == [0.6, 0.8]
    assert payloads[0]["input"] == ["title: Intro | text: A document"]
    assert payloads[1]["input"] == ["task: code retrieval | query: How?"]
    assert payloads[2]["messages"][0]["content"][0]["type"] == "image_url"


@pytest.fixture
def production_env(monkeypatch):
    for name in ("MINILM_TOKENIZER_PATH", "RUSTFS_ENDPOINT", "RUSTFS_ACCESS_KEY",
                 "RUSTFS_SECRET_KEY", "RUSTFS_BUCKET", "MILVUS_URI"):
        monkeypatch.setenv(name, "test-fixture")
    monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL_PATH", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL_DIR", raising=False)
    monkeypatch.setattr(milvus_sink, "MilvusIndexSink", Mock())
    monkeypatch.setattr(pipeline, "RustFSObjectStore", Mock())


def test_worker_defaults_to_remote_without_local_model(production_env, monkeypatch):
    monkeypatch.setenv("EMBEDDING_VLLM_URL", "http://shared:8000/v1")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "256")
    deps = pipeline.build_production_deps()
    assert isinstance(deps.embedding_backend, VllmEmbeddingBackend)
    assert deps.embedding_backend.base_url == "http://shared:8000/v1"
    assert deps.config.embeddings.dimension == deps.embedding_backend.dimension == 256


def test_worker_rejects_mock_backend(production_env, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "mock")
    with pytest.raises(RuntimeError, match="refuses mock"):
        pipeline.build_production_deps()
