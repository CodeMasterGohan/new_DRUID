"""Tests for the embedding service (spec §8).

Uses the mock backend (deterministic, dependency-free). No model downloads.
"""

from __future__ import annotations

import math
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("EMBEDDING_BACKEND", "mock")

from services.embedding import reuse  # noqa: E402
from services.embedding.service import EmbeddingService  # noqa: E402
from services.embedding.settings import EmbeddingServiceSettings  # noqa: E402

import services.embedding.main as main_module  # noqa: E402


@pytest.fixture()
def ing():
    return reuse.load_ingestion_embeddings()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "mock")
    monkeypatch.delenv("EMBEDDING_MODEL_PATH", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL_DIR", raising=False)
    with TestClient(main_module.app) as c:
        yield c


def _norm(vec):
    return math.sqrt(sum(v * v for v in vec))


# -- §8 prefix contract -----------------------------------------------------

def test_query_prefix_qa_default(ing):
    svc = EmbeddingService(EmbeddingServiceSettings(backend="mock"))
    assert svc.format_query(text="what is RAG?") == ing.build_query_text("what is RAG?", task="qa")
    assert svc.format_query(text="what is RAG?").startswith("task: question answering | query:")


def test_query_prefix_code_retrieval(ing):
    svc = EmbeddingService(EmbeddingServiceSettings(backend="mock"))
    formatted = svc.format_query(text="parse pdf", task="code_retrieval")
    assert formatted == ing.build_query_text("parse pdf", task="code")
    assert formatted.startswith("task: code retrieval | query:")


def test_query_prefix_search_result(ing):
    svc = EmbeddingService(EmbeddingServiceSettings(backend="mock"))
    formatted = svc.format_query(text="invoice 42", task="search_result")
    assert formatted == ing.build_query_text("invoice 42", task="search")
    assert formatted.startswith("task: search result | query:")


def test_document_prefix_byte_equal_to_sibling(ing):
    svc = EmbeddingService(EmbeddingServiceSettings(backend="mock"))
    chunk = ing.Chunk(
        chunk_id="g:c1",
        document_id="d1",
        document_version="v1",
        parent_id=None,
        page_start=0,
        page_end=0,
        chunk_order=0,
        heading_path=["Section", "Sub"],
        text="hello world",
        identifiers=[],
        metadata={},
    )
    expected = ing.build_document_text(chunk, filename="doc.pdf")
    got = svc.format_document(title="Section > Sub", text="hello world")
    assert got == expected
    assert got.startswith("title: Section > Sub | text:")


def test_reuse_loads_sibling_file_not_a_copy(ing):
    import pathlib

    assert pathlib.Path(ing.__file__).resolve().parent.name == "ingestion"
    # The contractual strings come from the sibling module object itself.
    assert "task: question answering | query:" in ing.PrefixContract.QUERY_QA


def test_prefix_selection_is_tested_not_assumed(client):
    # Different tasks must produce different vectors (mock is content-seeded),
    # proving the prefix actually flows into the embedding input.
    qa = client.post("/embed", json={"queries": ["parse pdf"], "task": "question_answering"})
    code = client.post("/embed", json={"queries": ["parse pdf"], "task": "code_retrieval"})
    assert qa.status_code == 200 and code.status_code == 200
    assert qa.json()["vectors"][0] != code.json()["vectors"][0]


# -- HTTP surface ------------------------------------------------------------

def test_embed_documents_and_queries(client):
    resp = client.post(
        "/embed",
        json={
            "documents": [{"text": "some prose", "title": "Intro"}],
            "queries": ["what is this?"],
            "task": "question_answering",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["vectors"]) == 2
    assert body["dimension"] == 768
    assert body["normalized"] is True
    assert body["model"] == "google/embeddinggemma-2"
    for vec in body["vectors"]:
        assert len(vec) == 768
        assert abs(_norm(vec) - 1.0) < 1e-6


def test_embed_rejects_empty(client):
    resp = client.post("/embed", json={})
    assert resp.status_code == 400


def test_embed_image_page_image_metadata(client):
    import base64
    import io

    # Minimal valid PNG (1x1) — the mock backend hashes bytes, no PIL needed
    # for the mock path... but the service passes bytes straight through.
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    _ = io  # (kept for clarity: bytes are a real PNG)
    resp = client.post(
        "/embed_image",
        json={"image_b64": base64.b64encode(png).decode(), "page_label": "Page 3 of doc.pdf"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata"] == {"modality": "page_image"}
    assert body["text"] == "Page 3 of doc.pdf"  # label, never OCR output
    assert len(body["vector"]) == 768
    assert abs(_norm(body["vector"]) - 1.0) < 1e-6


def test_embed_image_rejects_bad_base64(client):
    resp = client.post("/embed_image", json={"image_b64": "!!!not-base64!!!", "page_label": "x"})
    assert resp.status_code == 400


def test_embed_image_object_key_not_wired(client):
    import base64

    resp = client.post(
        "/embed_image",
        json={"image_b64": base64.b64encode(b"fakepng").decode(), "object_key": "renders/x.png", "page_label": "x"},
    )
    assert resp.status_code == 501


def test_health_reports_card(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["model"] == "google/embeddinggemma-2"
    assert body["modality_config"] == "text+image"
    assert body["dimension"] == 768
    assert body["device"] == "cpu"
    assert body["model_loaded"] is True


# -- fail-closed behavior -----------------------------------------------------

def test_startup_fails_without_artifact(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "st")
    monkeypatch.setenv("EMBEDDING_MODEL_PATH", "/nonexistent/models/embeddinggemma-2")
    with pytest.raises(RuntimeError):
        with TestClient(main_module.app):
            pass  # pragma: no cover


def test_startup_fails_without_path_env(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "st")
    monkeypatch.delenv("EMBEDDING_MODEL_PATH", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL_DIR", raising=False)
    with pytest.raises(RuntimeError):
        with TestClient(main_module.app):
            pass  # pragma: no cover


def test_width_guard_rejects_unvalidated_width():
    with pytest.raises(ValueError, match="Matryoshka"):
        EmbeddingServiceSettings(backend="mock", dimension=100)


def test_validated_matryoshka_width_accepted():
    svc = EmbeddingService(EmbeddingServiceSettings(backend="mock", dimension=256))
    assert svc.settings.dimension == 256
    assert len(svc._backend.embed_texts(["x"])[0]) == 256


def test_input_char_guard(client):
    long_text = "x" * 40000
    resp = client.post("/embed", json={"queries": [long_text]})
    assert resp.status_code == 400
    assert "pre-chunked" in resp.json()["detail"]


def test_embedding_service_deduplicates_and_caches_repeated_texts(monkeypatch):
    svc = EmbeddingService(EmbeddingServiceSettings(backend="mock"))
    calls: list[list[str]] = []
    orig = svc._backend.embed_texts

    def spy_embed_texts(texts):
        calls.append(list(texts))
        return orig(texts)

    monkeypatch.setattr(svc._backend, "embed_texts", spy_embed_texts)
    import asyncio

    vecs1 = asyncio.run(svc.embed_queries(["same query", "same query", "other query"]))
    assert len(vecs1) == 3
    assert vecs1[0] == vecs1[1]
    assert len(calls) == 1
    assert len(calls[0]) == 2  # deduplicated before backend call

    # Second call with already-cached queries hits the LRU cache without backend invocation.
    vecs2 = asyncio.run(svc.embed_queries(["same query", "other query"]))
    assert vecs2 == [vecs1[0], vecs1[2]]
    assert len(calls) == 1
