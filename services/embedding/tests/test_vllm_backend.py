"""Exercise the actual vLLM HTTP contract without a GPU or model downloads."""

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from services.embedding import main, service
from services.embedding.settings import EmbeddingServiceSettings
from services.embedding.vllm_backend import VllmEmbeddingBackend


def _body(*, count=1, dimension=768):
    return {
        "model": "google/embeddinggemma-2",
        "data": [
            {"index": i, "embedding": [3.0, 4.0] + [0.0] * (dimension - 2)}
            for i in range(count)
        ],
    }


def test_remote_backend_preserves_inputs_order_and_normalizes_low_precision():
    def handle(request):
        assert str(request.url) == "http://shared:8000/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {
            "model": "google/embeddinggemma-2",
            "input": ["title: Intro | text: hello", "task: question answering | query: hello"],
            "encoding_format": "float",
            "add_special_tokens": True,
        }
        body = _body(count=2)
        body["data"][1]["embedding"][:2] = [4.0, 3.0]
        body["data"].reverse()
        return httpx.Response(200, json=body)

    backend = VllmEmbeddingBackend(
        base_url="http://shared:8000/v1/", api_key="test-key", dimension=256,
        transport=httpx.MockTransport(handle),
    )
    vectors = backend.embed_texts([
        "title: Intro | text: hello", "task: question answering | query: hello",
    ])
    assert vectors[0][:2] == [0.6, 0.8]
    assert vectors[1][:2] == [0.8, 0.6]
    assert all(len(v) == 256 for v in vectors)


def test_image_uses_multimodal_messages_and_special_tokens():
    image = b"\x89PNG\r\n\x1a\npage-render"

    def handle(request):
        payload = json.loads(request.content)
        assert "input" not in payload
        assert payload["add_special_tokens"] is True
        assert payload["messages"] == [{
            "role": "user", "content": [{
                "type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(image).decode(),
                },
            }],
        }]
        return httpx.Response(200, json=_body())

    backend = VllmEmbeddingBackend(transport=httpx.MockTransport(handle))
    assert backend.embed_images([image])[0][:2] == [0.6, 0.8]
    with pytest.raises(ValueError, match="image embedding requires"):
        backend.embed_images([b"not-an-image"])


@pytest.mark.parametrize("change, message", [
    (lambda b: b.update(model="another-model"), "model"),
    (lambda b: b.update(data=[]), "count"),
    (lambda b: b["data"][0].update(index=1), "indices"),
    (lambda b: b["data"][0].update(index=True), "indices"),
    (lambda b: b["data"][0].update(embedding=[1.0]), "width"),
    (lambda b: b["data"][0]["embedding"].__setitem__(0, "1.0"), "non-finite"),
    (lambda b: b["data"][0]["embedding"].__setitem__(0, True), "non-finite"),
    (lambda b: b["data"][0].update(embedding=[0.0] * 768), "zero"),
])
def test_remote_response_must_match_contract(change, message):
    body = _body()
    change(body)
    backend = VllmEmbeddingBackend(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=body),
    ))
    with pytest.raises(RuntimeError, match=message):
        backend.embed_texts(["one"])


def test_duplicate_indices_are_rejected():
    body = _body(count=2)
    body["data"][1]["index"] = 0
    backend = VllmEmbeddingBackend(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=body),
    ))
    with pytest.raises(RuntimeError, match="indices"):
        backend.embed_texts(["one", "two"])


@pytest.mark.parametrize("raw", [b"not-json", b'{"model":"google/embeddinggemma-2","data":[{"index":0,"embedding":[NaN]}]}'])
def test_invalid_json_or_nonfinite_response_is_rejected(raw):
    backend = VllmEmbeddingBackend(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=raw),
    ))
    with pytest.raises(RuntimeError):
        backend.embed_texts(["one"])


@pytest.mark.parametrize("status", [401, 404, 503])
def test_remote_errors_do_not_fall_back_or_expose_response(status):
    backend = VllmEmbeddingBackend(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={"detail": "private document and key"}),
    ))
    with pytest.raises(RuntimeError, match=f"HTTP {status}") as exc:
        backend.embed_texts(["one"])
    assert "private" not in str(exc.value)


def test_default_service_forwards_existing_api_with_prefixes(monkeypatch):
    monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)
    monkeypatch.setenv("EMBEDDING_VLLM_URL", "http://shared:8000/v1")
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_body(count=len(calls[-1]["input"])))

    def factory(**kwargs):
        return VllmEmbeddingBackend(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(service, "VllmEmbeddingBackend", factory)
    with TestClient(main.app) as client:
        health = client.get("/health").json()
        assert health["backend"] == "vllm"
        assert health["device"] == "remote"
        assert health["model_loaded"] is True
        assert health["vllm_url"] == "http://shared:8000/v1"
        response = client.post("/embed", json={"queries": ["how?"], "task": "code_retrieval"})
        assert response.status_code == 200
        assert response.json()["vectors"][0][:2] == [0.6, 0.8]
        response = client.post("/embed", json={
            "documents": [{"title": "Intro", "text": "example"}],
        })
        assert response.status_code == 200
    assert calls[0]["input"] == ["druid warmup"]
    assert calls[1]["input"] == ["task: code retrieval | query: how?"]
    assert calls[2]["input"] == ["title: Intro | text: example"]


def test_remote_unavailable_fails_startup(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "vllm")

    def unavailable(request):
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(service, "VllmEmbeddingBackend", lambda **kwargs:
        VllmEmbeddingBackend(**kwargs, transport=httpx.MockTransport(unavailable)))
    with pytest.raises(RuntimeError, match="unavailable"):
        with TestClient(main.app):
            pass


def test_settings_default_to_shared_model_without_local_artifacts(monkeypatch):
    monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)
    monkeypatch.delenv("EMBEDDING_VLLM_URL", raising=False)
    settings = EmbeddingServiceSettings()
    assert settings.backend == "vllm"
    assert settings.vllm_url == "http://vllm-embedding:8000/v1"
    assert settings.model_id == "google/embeddinggemma-2"
