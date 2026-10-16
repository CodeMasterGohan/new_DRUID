"""Production adapter contract tests: models and Milvus are faked, no network."""
import httpx
import pytest

from services.retrieval.predicates import AuthPredicate
from services.retrieval.production import HttpEmbeddingClient, MilvusVectorStore


def test_embed_client_strips_existing_prompt_exactly_once(monkeypatch):
    observed = []
    transport = httpx.MockTransport(lambda request: (
        observed.append(request) or httpx.Response(200, json={
            "vectors": [[0.5] * 768], "dimension": 768
        })
    ))
    original_client = httpx.Client
    class Client:
        def __init__(self, timeout):
            self.client = httpx.Client(transport=transport)
        def __enter__(self):
            return self.client
        def __exit__(self, *args):
            self.client.close()
    # Save the unpatched constructor: our fake must not recurse.
    monkeypatch.setattr("services.retrieval.production.httpx.Client", Client)
    adapter = HttpEmbeddingClient("http://embedding:8003")
    vec = adapter.embed_query("task: code retrieval | query: find useEffect()")
    assert len(vec) == 768
    assert b'"queries":["find useEffect()"]' in observed[0].content
    assert b'"code_retrieval"' in observed[0].content
    with pytest.raises(ValueError, match="unexpected query prefix"):
        adapter.embed_query("unprefixed topic")


def test_milvus_branches_never_drop_tenant_or_version_filter():
    class FakeClient:
        def __init__(self):
            self.searches = []
            self.queries = []
        def search(self, **kwargs):
            self.searches.append(kwargs)
            return [[]]
        def query(self, **kwargs):
            self.queries.append(kwargs)
            return []
    client = FakeClient()
    store = MilvusVectorStore("http://milvus:19530", "project_knowledge_base", client=client)
    predicate = AuthPredicate(
        "tenant-a", frozenset({("doc-1", "1")})
    )
    store.dense_search([0.1] * 768, predicate, 10)
    store.bm25_search("DOC-ABC-100", predicate, 10)
    store.identifier_search(["DOC-ABC-100"], predicate, 10)
    filters = [x["filter"] for x in client.searches + client.queries]
    assert len(filters) == 3
    assert all('group_id == "tenant-a"' in expr for expr in filters)
    assert all('document_version == "1"' in expr for expr in filters)
    assert "ARRAY_CONTAINS_ANY" in filters[2]
    assert client.searches[1]["anns_field"] == "sparse_vector"


def test_empty_predicate_rejects_every_branch():
    class NeverCalled:
        def search(self, **kwargs):
            raise AssertionError("vector access leaked")
        def query(self, **kwargs):
            raise AssertionError("vector access leaked")
    s = MilvusVectorStore("http://milvus:19530", "kb", client=NeverCalled())
    pred = AuthPredicate("tenant-a", frozenset())
    with pytest.raises(ValueError):
        s.dense_search([0.1] * 768, pred, 1)
    with pytest.raises(ValueError):
        s.bm25_search("hello", pred, 1)
    with pytest.raises(ValueError):
        s.identifier_search(["INV-1000"], pred, 1)


def test_milvus_maps_page_image_provenance():
    record = MilvusVectorStore._record({
        "chunk_id": "tenant-a:img", "document_id": "doc-1",
        "document_version": "2", "text": "page 2", "page_start": 2,
        "page_end": 2, "chunk_order": 1, "metadata": {
            "modality": "page_image", "filename": "drawing.pdf",
            "render_ref": "tenant-a/img2.png",
            "detection_provenance": "olmocr-routed",
        },
    })
    assert record.filename == "drawing.pdf"
    assert record.modality == "page_image"
    assert record.page_image_ref == "tenant-a/img2.png"
    assert record.metadata["chunk_order"] == 1
