"""Transport and adapter contracts; no model downloads or GPU required."""
import asyncio
import io
import json
import math
import threading

import httpx
import pytest
from PIL import Image

from services.embedding.service import EmbeddingService
from services.embedding.settings import EmbeddingServiceSettings
from services.embedding.vllm_backend import VllmEmbeddingBackend


def settings(**kwargs):
    return EmbeddingServiceSettings(backend="vllm", **kwargs)


def response(vectors):
    return {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}


def backend(handler, **kwargs):
    client = httpx.Client(base_url="http://embedding/v1/", transport=httpx.MockTransport(handler))
    return VllmEmbeddingBackend(settings(**kwargs), client=client)


def test_reorders_batches_and_normalizes_truncated_vectors():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        vectors = [[float(int(t))] + [1.0] * 767 for t in body["input"]]
        data = response(vectors)
        data["data"].reverse()
        return httpx.Response(200, json=data)

    b = backend(handler, dimension=256, batch_size=2)
    vectors = b.embed_texts(["1", "2", "3"])
    assert [len(r["input"]) for r in seen] == [2, 1]
    assert all(r["dimensions"] == 768 for r in seen)
    assert all(len(v) == 256 for v in vectors)
    assert all(math.isclose(sum(x*x for x in v), 1.0) for v in vectors)
    assert vectors[0][0] < vectors[1][0] < vectors[2][0]
    b.close()


@pytest.mark.parametrize("bad", [
    {}, {"data": []},
    {"data": [{"index": 0, "embedding": [1.0] * 512}]},
    {"data": [{"index": 1, "embedding": [1.0] * 768}]},
    {"data": [{"index": 0, "embedding": [0.0] * 768}]},
    {"data": [{"index": 0, "embedding": [True] * 768}]},
    {"data": [{"index": 0, "embedding": ["nan"] * 768}]},
])
def test_rejects_malformed_responses(bad):
    b = backend(lambda req: httpx.Response(200, json=bad))
    with pytest.raises(RuntimeError, match="invalid embedding"):
        b.embed_texts(["private input"])
    b.close()


def test_rejects_duplicate_indices_and_nonfinite_vectors():
    for data in (
        {"data": [{"index": 0, "embedding": [1.0]*768}] * 2},
        {"data": [{"index": i, "embedding": [float("nan")]*768} for i in range(2)]},
    ):
        b = backend(lambda req: httpx.Response(200, content=json.dumps(data)))
        with pytest.raises(RuntimeError, match="invalid embedding"):
            b.embed_texts(["a", "b"])
        b.close()


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_upstream_errors_are_sanitized(status):
    b = backend(lambda req: httpx.Response(status, text="secret document content"))
    with pytest.raises(RuntimeError, match=f"HTTP {status}") as error:
        b.embed_texts(["private input"])
    assert "secret" not in str(error.value)
    b.close()


def test_timeout_is_fail_closed():
    def timeout(req):
        raise httpx.ReadTimeout("timeout", request=req)

    b = backend(timeout)
    with pytest.raises(RuntimeError, match="request failed"):
        b.embed_texts(["x"])
    b.close()


def test_page_image_has_no_label_or_task_prefix():
    seen = []
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=response([[1.0]*768]))

    png = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(png, "PNG")
    b = backend(handler)
    assert len(b.embed_images([png.getvalue()])[0]) == 768
    body = seen[0]
    content = body["messages"][0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert body["add_generation_prompt"] is False
    with pytest.raises(ValueError, match="invalid page image"):
        b.embed_images([b"invalid"])
    b.close()
    text_only = backend(handler, modality_config="text-only")
    with pytest.raises(ValueError, match="text\\+image"):
        text_only.embed_images([png.getvalue()])
    text_only.close()


def test_authentication_and_readiness(monkeypatch):
    requests = []
    def handler(req):
        requests.append(req)
        return httpx.Response(200, json={})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(
        **kw, transport=httpx.MockTransport(handler)
    ))
    b = VllmEmbeddingBackend(settings(vllm_api_key="test-key"))
    b.check_ready()
    assert requests[0].url.path == "/health"
    assert requests[0].headers["Authorization"] == "Bearer test-key"
    assert "test-key" not in repr(b.settings)
    b.close()


def test_adapter_applies_prefix_once_and_allows_parallel_remote_calls():
    barrier = threading.Barrier(2, timeout=5)
    seen = []
    def handler(req):
        seen.extend(json.loads(req.content)["input"])
        barrier.wait()
        return httpx.Response(200, json=response([[1.0]*768]))

    svc = EmbeddingService(settings())
    svc.backend.close()
    svc._backend = backend(handler)
    async def run():
        return await asyncio.gather(
            svc.embed_queries(["question"], task="code_retrieval"),
            svc.embed_documents([{"title": "Title", "text": "Body"}]),
        )

    results = asyncio.run(run())
    assert all(len(v[0]) == 768 for v in results)
    assert sorted(seen) == sorted([
        "task: code retrieval | query: question", "title: Title | text: Body",
    ])
    svc.close()


def test_adapter_checks_every_vector_after_warmup():
    svc = EmbeddingService(EmbeddingServiceSettings(backend="mock"))
    svc._verify_vector([1.0] + [0.0]*767)
    with pytest.raises(RuntimeError, match="non-finite"):
        svc._verify_vector([float("nan")]*768)
    with pytest.raises(RuntimeError, match="width"):
        svc._verify_vector([1.0])


def test_api_health_detects_remote_engine_failure(monkeypatch):
    from fastapi.testclient import TestClient
    from services.embedding import main

    alive = [True]
    def handler(req):
        return httpx.Response(200 if alive[0] else 503)

    svc = EmbeddingService(EmbeddingServiceSettings(backend="mock"))
    svc.settings = settings()
    svc._backend = backend(handler)
    svc._verified = True
    monkeypatch.setattr(main, "service", svc)
    try:
        client = TestClient(main.app)
        assert client.get("/health").json()["device"] == "cuda:0 (remote vLLM)"
        alive[0] = False
        assert client.get("/health").status_code == 503
    finally:
        svc.close()
