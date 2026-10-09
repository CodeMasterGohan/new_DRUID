"""Tests for the production HTTP service clients (mocked httpx transport).

Every downstream call is intercepted by ``httpx.MockTransport`` — no real
network, no real backends. The tests assert:

* request shapes (paths, query params, JSON bodies, signed identity headers),
* response mapping onto the ABC value objects,
* error mapping (404 -> NotFoundError, everything else -> ServiceClientError),
* the documented ingestion gaps (NotImplementedError),
* ``create_app`` wiring: real clients by default, fakes behind the flag.
"""

from __future__ import annotations

import time

import httpx
import pytest

from mcp_server.clients import (
    InMemoryCatalogClient,
    InMemoryIngestionClient,
    InMemoryRetrievalClient,
    NotFoundError,
)
from mcp_server.context import RequestContext
from mcp_server.http_clients import (
    HttpCatalogClient,
    HttpIngestionClient,
    HttpRetrievalClient,
    ServiceClientError,
    sign_document_api_headers,
    sign_retrieval_headers,
)
from mcp_server.server import Settings, _default_deps, create_app

SECRET = "test-internal-secret"
RETRIEVAL_URL = "http://retrieval:8004"
DOCUMENT_API_URL = "http://document_api:8001"


def make_ctx(**overrides) -> RequestContext:
    kwargs = {
        "user_id": "u1",
        "group_id": "acme",
        "roles": frozenset({"member"}),
        "request_id": "req-123",
    }
    kwargs.update(overrides)
    return RequestContext(**kwargs)


def make_transport(handler) -> tuple[httpx.MockTransport, list]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped), seen


def json_response(
    status: int, payload: dict, request: httpx.Request
) -> httpx.Response:
    return httpx.Response(status, json=payload, request=request)


# ---------------------------------------------------------------------------
# fixtures: sample downstream payloads
# ---------------------------------------------------------------------------


def sample_hit(**overrides) -> dict:
    hit = {
        "document_id": "d1",
        "document_version": "v3",
        "chunk_id": "d1_c7",
        "filename": "Acme 10-K.pdf",
        "page_start": 2,
        "page_end": 3,
        "heading_path": ["Risk Factors"],
        "text": "The quick brown fox.",
        "rank": 1,
        "reranker_score": 0.91,
        "source_reference": "Acme 10-K.pdf (pp. 2-3)",
        "provenance": "olmocr-routed",
        "modality": "text",
        "page_image_ref": None,
        "trust": "untrusted-document-content",
    }
    hit.update(overrides)
    return hit


def sample_doc_detail(document_id: str, **overrides) -> dict:
    detail = {
        "document_id": document_id,
        "display_name": f"{document_id}.pdf",
        "checksum_sha256": "ab" * 32,
        "active_version_id": "v3",
        "is_deleted": False,
        "created_at": "2026-10-01T12:00:00Z",
        "updated_at": "2026-10-02T12:00:00Z",
        "versions": [
            {
                "version_id": "v3",
                "version_number": 3,
                "status": "READY",
                "parser": "mixed",
                "parser_revision": "router-v1",
                "page_count": 10,
                "chunk_count_actual": 42,
                "is_active": True,
                "created_at": "2026-10-01T12:00:00Z",
            }
        ],
        "artifacts": {
            "original": {
                "key": f"acme/{document_id}/v3/original",
                "download_url": "http://rustfs/presigned",
            }
        },
    }
    detail.update(overrides)
    return detail


def sample_review_item(**overrides) -> dict:
    item = {
        "item_id": "ri-1",
        "document_id": "d1",
        "document_version_id": "v3",
        "page_index": 4,
        "status": "pending",
        "page_render_key": "acme/d1/renders/page-0004.png",
        "page_render_url": "http://rustfs/presigned-render",
        "parse_attempts": {
            "native": {"text": "native extracted text here"},
            "visual": {"error": "low quality render"},
        },
        "routing_diagnostics": {"text_density": 0.12, "has_embedded_text": False},
        "reviewer_decision": None,
        "reviewed_by": None,
        "reviewed_at": None,
        "created_at": "2026-10-03T12:00:00Z",
    }
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# header signing
# ---------------------------------------------------------------------------


class TestSigning:
    def test_document_api_headers_shape(self):
        headers = sign_document_api_headers(make_ctx(), SECRET)
        assert headers["X-Druid-User-Id"] == "u1"
        assert headers["X-Druid-Group-Id"] == "acme"
        assert headers["X-Druid-Roles"] == "member"
        assert headers["X-Druid-Request-Id"] == "req-123"
        sig = headers["X-Druid-Internal-Sig"]
        assert sig.startswith("v1:") and len(sig) == 3 + 64

    def test_document_api_roles_sorted(self):
        ctx = make_ctx(roles=frozenset({"reviewer", "member", "admin"}))
        headers = sign_document_api_headers(ctx, SECRET)
        assert headers["X-Druid-Roles"] == "admin,member,reviewer"

    def test_retrieval_headers_shape(self):
        before = time.time()
        headers = sign_retrieval_headers(make_ctx(), SECRET)
        assert headers["X-Druid-User"] == "u1"
        assert headers["X-Druid-Group"] == "acme"
        assert headers["X-Druid-Request-Id"] == "req-123"
        assert len(headers["X-Druid-Signature"]) == 64
        ts = float(headers["X-Druid-Timestamp"])
        assert before - 5 <= ts <= time.time() + 5  # fresh timestamp


# ---------------------------------------------------------------------------
# retrieval
# ---------------------------------------------------------------------------


class TestHttpRetrievalClient:
    async def test_search_success_maps_provenance(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/search"
            assert request.method == "POST"
            import json as _json

            assert _json.loads(request.content) == {"query": "fox"}
            assert "x-druid-signature" in request.headers
            ts = float(request.headers["x-druid-timestamp"])
            assert abs(time.time() - ts) < 300
            return json_response(
                200,
                {"results": [sample_hit()], "passages": ["ignored"]},
                request,
            )

        transport, _ = make_transport(handler)
        client = HttpRetrievalClient(RETRIEVAL_URL, SECRET, transport=transport)
        passages = await client.search(make_ctx(), query="fox", top_k=8)
        assert len(passages) == 1
        prov = passages[0].provenance
        assert prov.document_id == "d1"
        assert prov.filename == "Acme 10-K.pdf"
        assert prov.chunk_id == "d1_c7"
        assert (prov.page_start, prov.page_end) == (2, 3)
        assert prov.modality == "text"
        assert prov.parser == "olmocr-routed"
        assert prov.source_reference == "Acme 10-K.pdf (pp. 2-3)"
        assert prov.page_image_ref is None
        assert passages[0].text == "The quick brown fox."

    async def test_search_applies_top_k_client_side(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response(
                200,
                {"results": [sample_hit(chunk_id=f"c{i}") for i in range(5)]},
                request,
            )

        transport, _ = make_transport(handler)
        client = HttpRetrievalClient(RETRIEVAL_URL, SECRET, transport=transport)
        passages = await client.search(make_ctx(), query="fox", top_k=2)
        assert [p.provenance.chunk_id for p in passages] == ["c0", "c1"]

    async def test_search_unauthorized_maps_to_service_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response(401, {"detail": "bad identity signature"}, request)

        transport, _ = make_transport(handler)
        client = HttpRetrievalClient(RETRIEVAL_URL, SECRET, transport=transport)
        with pytest.raises(ServiceClientError, match="401"):
            await client.search(make_ctx(), query="fox", top_k=8)

    async def test_search_malformed_payload(self):
        for payload in ({"nope": True}, {"results": "not-a-list"},
                        {"results": [{"document_id": "x"}]}):
            def handler(request, _p=payload):
                return json_response(200, _p, request)

            transport, _ = make_transport(handler)
            client = HttpRetrievalClient(
                RETRIEVAL_URL, SECRET, transport=transport
            )
            with pytest.raises(ServiceClientError):
                await client.search(make_ctx(), query="fox", top_k=8)

    async def test_search_connection_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        transport, _ = make_transport(handler)
        client = HttpRetrievalClient(RETRIEVAL_URL, SECRET, transport=transport)
        with pytest.raises(ServiceClientError, match="request failed"):
            await client.search(make_ctx(), query="fox", top_k=8)

    async def test_search_missing_secret_fails_closed(self):
        client = HttpRetrievalClient(RETRIEVAL_URL, "")
        with pytest.raises(ServiceClientError, match="INTERNAL_SECRET"):
            await client.search(make_ctx(), query="fox", top_k=8)


# ---------------------------------------------------------------------------
# catalog: list
# ---------------------------------------------------------------------------


class TestHttpCatalogList:
    def _list_transport(self, docs: list[dict], seen: list):
        details = {d["document_id"]: sample_doc_detail(d["document_id"])
                   for d in docs}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/v1/documents":
                return json_response(
                    200,
                    {"documents": docs, "limit": 50, "offset": 0},
                    request,
                )
            if request.url.path.startswith("/v1/documents/"):
                doc_id = request.url.path.rsplit("/", 1)[-1]
                return json_response(200, details[doc_id], request)
            return json_response(404, {"detail": "not found"}, request)

        return httpx.MockTransport(handler)

    async def test_list_enriches_with_detail(self):
        seen: list = []
        transport = self._list_transport(
            [{"document_id": "d1"}, {"document_id": "d2"}], seen
        )
        client = HttpCatalogClient(
            DOCUMENT_API_URL, SECRET, transport=transport
        )
        page = await client.list_documents(
            make_ctx(), review_required=True, status=None, limit=50, offset=0
        )
        assert page.total == 2  # offset + len(page); exact (short page)
        assert [d.document_id for d in page.items] == ["d1", "d2"]
        first = page.items[0]
        assert first.filename == "d1.pdf"
        assert first.status == "READY"
        assert first.version == "3"
        assert first.page_count == 10
        assert first.created_at == "2026-10-01T12:00:00Z"
        list_req = next(r for r in seen if r.url.path == "/v1/documents")
        assert list_req.url.params["review_required"] == "true"
        assert list_req.url.params["limit"] == "50"
        # signed identity headers on every call, including enrichment
        for req in seen:
            assert req.headers["X-Druid-Internal-Sig"].startswith("v1:")

    async def test_list_status_review_required_maps_to_filter(self):
        seen: list = []
        transport = self._list_transport([{"document_id": "d1"}], seen)
        client = HttpCatalogClient(
            DOCUMENT_API_URL, SECRET, transport=transport
        )
        await client.list_documents(
            make_ctx(), review_required=None, status="REVIEW_REQUIRED",
            limit=50, offset=0,
        )
        list_req = next(r for r in seen if r.url.path == "/v1/documents")
        assert list_req.url.params["review_required"] == "true"

    async def test_list_other_status_filters_client_side(self):
        seen: list = []

        def detail(doc_id: str, status: str) -> dict:
            d = sample_doc_detail(doc_id)
            d["versions"][0]["status"] = status
            return d

        details = {"d1": detail("d1", "READY"), "d2": detail("d2", "FAILED")}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/v1/documents":
                # document_api has no status filter: must NOT be sent
                assert "status" not in request.url.params
                return json_response(
                    200,
                    {"documents": [{"document_id": "d1"},
                                   {"document_id": "d2"}],
                     "limit": 50, "offset": 0},
                    request,
                )
            doc_id = request.url.path.rsplit("/", 1)[-1]
            return json_response(200, details[doc_id], request)

        client = HttpCatalogClient(
            DOCUMENT_API_URL, SECRET, transport=httpx.MockTransport(handler)
        )
        page = await client.list_documents(
            make_ctx(), review_required=None, status="FAILED",
            limit=50, offset=0,
        )
        assert [d.document_id for d in page.items] == ["d2"]

    async def test_list_upstream_404(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response(404, {"detail": "nope"}, request)

        client = HttpCatalogClient(
            DOCUMENT_API_URL, SECRET,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(NotFoundError):
            await client.list_documents(
                make_ctx(), review_required=None, status=None,
                limit=50, offset=0,
            )


# ---------------------------------------------------------------------------
# catalog: get document
# ---------------------------------------------------------------------------


class TestHttpCatalogGet:
    async def test_get_document_with_artifacts_uses_opaque_keys(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/documents/d1"
            return json_response(200, sample_doc_detail("d1"), request)

        client = HttpCatalogClient(
            DOCUMENT_API_URL, SECRET,
            transport=httpx.MockTransport(handler),
        )
        doc = await client.get_document(
            make_ctx(), document_id="d1", include_artifacts=True
        )
        assert doc.filename == "d1.pdf"
        assert doc.status == "READY"
        # opaque object-storage key — never the presigned URL
        assert doc.artifact_refs == {"original": "acme/d1/v3/original"}

    async def test_get_document_without_artifacts(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response(200, sample_doc_detail("d1"), request)

        client = HttpCatalogClient(
            DOCUMENT_API_URL, SECRET,
            transport=httpx.MockTransport(handler),
        )
        doc = await client.get_document(
            make_ctx(), document_id="d1", include_artifacts=False
        )
        assert doc.artifact_refs == {}

    async def test_get_document_404(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response(404, {"detail": "not found"}, request)

        client = HttpCatalogClient(
            DOCUMENT_API_URL, SECRET,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(NotFoundError, match="unknown document: d9"):
            await client.get_document(
                make_ctx(), document_id="d9", include_artifacts=False
            )


# ---------------------------------------------------------------------------
# catalog: review
# ---------------------------------------------------------------------------


class TestHttpCatalogReview:
    async def test_get_review_item(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/review/items/ri-1"
            return json_response(200, sample_review_item(), request)

        client = HttpCatalogClient(
            DOCUMENT_API_URL, SECRET,
            transport=httpx.MockTransport(handler),
        )
        item = await client.get_review_item(make_ctx(), review_item_id="ri-1")
        assert item.review_item_id == "ri-1"
        assert item.document_id == "d1"
        assert item.page_index == 4
        # opaque render key, not the presigned URL
        assert item.page_render_ref == "acme/d1/renders/page-0004.png"
        assert "native extracted text" in item.native_attempt_summary
        assert "low quality render" in item.visual_attempt_summary
        assert item.routing_diagnostics == {
            "text_density": "0.12",
            "has_embedded_text": "False",
        }

    async def test_get_review_item_404(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response(404, {"detail": "not found"}, request)

        client = HttpCatalogClient(
            DOCUMENT_API_URL, SECRET,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(NotFoundError, match="unknown review item"):
            await client.get_review_item(make_ctx(), review_item_id="nope")

    async def test_resolve_review_item_maps_decision(self):
        seen: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            assert request.url.path == "/v1/review/items/ri-1/resolve"
            import json as _json

            body = _json.loads(request.content)
            assert body == {
                "decision": "re-routed",
                "detail": {"note": "looks fine"},
            }
            return json_response(
                200,
                {"item_id": "ri-1", "status": "re-routed",
                 "reviewed_by": "u1"},
                request,
            )

        client = HttpCatalogClient(
            DOCUMENT_API_URL, SECRET,
            transport=httpx.MockTransport(handler),
        )
        new_state = await client.resolve_review_item(
            make_ctx(), review_item_id="ri-1", decision="reroute",
            note="looks fine",
        )
        assert new_state == "re-routed"
        assert len(seen) == 1

    async def test_resolve_review_item_invalid_decision(self):
        client = HttpCatalogClient(DOCUMENT_API_URL, SECRET)
        with pytest.raises(ValueError, match="unknown review decision"):
            await client.resolve_review_item(
                make_ctx(), review_item_id="ri-1", decision="maybe",
                note=None,
            )

    async def test_resolve_review_item_conflict(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response(409, {"detail": "already resolved"}, request)

        client = HttpCatalogClient(
            DOCUMENT_API_URL, SECRET,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(ServiceClientError, match="409"):
            await client.resolve_review_item(
                make_ctx(), review_item_id="ri-1", decision="accept",
                note=None,
            )

    async def test_resolve_review_item_404(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response(404, {"detail": "not found"}, request)

        client = HttpCatalogClient(
            DOCUMENT_API_URL, SECRET,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(NotFoundError):
            await client.resolve_review_item(
                make_ctx(), review_item_id="nope", decision="accept",
                note=None,
            )


# ---------------------------------------------------------------------------
# catalog: delete
# ---------------------------------------------------------------------------


class TestHttpCatalogDelete:
    async def test_delete_returns_job_state(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "DELETE"
            assert request.url.path == "/v1/documents/d1"
            return json_response(
                202,
                {"document_id": "d1", "job_id": "j1", "status": "QUEUED"},
                request,
            )

        client = HttpCatalogClient(
            DOCUMENT_API_URL, SECRET,
            transport=httpx.MockTransport(handler),
        )
        assert (
            await client.delete_document(make_ctx(), document_id="d1")
            == "QUEUED"
        )

    async def test_delete_404(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response(404, {"detail": "not found"}, request)

        client = HttpCatalogClient(
            DOCUMENT_API_URL, SECRET,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(NotFoundError, match="unknown document"):
            await client.delete_document(make_ctx(), document_id="d9")


# ---------------------------------------------------------------------------
# ingestion gaps
# ---------------------------------------------------------------------------


class TestHttpIngestionGaps:
    async def test_register_upload_has_no_backing_endpoint(self):
        client = HttpIngestionClient(DOCUMENT_API_URL, SECRET)
        with pytest.raises(NotImplementedError, match="no backing endpoint"):
            await client.register_upload(
                make_ctx(),
                filename="a.pdf",
                mime_type="application/pdf",
                file_size_bytes=10,
                content_sha256="ab" * 32,
            )

    async def test_get_job_status_has_no_backing_endpoint(self):
        client = HttpIngestionClient(DOCUMENT_API_URL, SECRET)
        with pytest.raises(NotImplementedError, match="no backing endpoint"):
            await client.get_job_status(make_ctx(), job_id="j1")


# ---------------------------------------------------------------------------
# server wiring
# ---------------------------------------------------------------------------


def _real_settings(**overrides) -> Settings:
    kwargs = {
        "jwt_secret": "test-jwt-secret",
        "retrieval_url": RETRIEVAL_URL,
        "document_api_url": DOCUMENT_API_URL,
        "internal_secret": SECRET,
        "dev_fake_clients": False,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


class TestServerWiring:
    def test_fakes_behind_flag(self):
        deps = _default_deps(_real_settings(dev_fake_clients=True))
        assert isinstance(deps.retrieval, InMemoryRetrievalClient)
        assert isinstance(deps.ingestion, InMemoryIngestionClient)
        assert isinstance(deps.catalog, InMemoryCatalogClient)

    def test_real_clients_by_default(self):
        deps = _default_deps(_real_settings())
        assert isinstance(deps.retrieval, HttpRetrievalClient)
        assert isinstance(deps.ingestion, HttpIngestionClient)
        assert isinstance(deps.catalog, HttpCatalogClient)

    def test_real_clients_missing_config_fails_closed(self):
        with pytest.raises(RuntimeError, match="INTERNAL_SECRET"):
            _default_deps(_real_settings(internal_secret=""))
        with pytest.raises(RuntimeError, match="RETRIEVAL_URL"):
            _default_deps(_real_settings(retrieval_url=""))

    def test_create_app_builds_with_real_clients(self):
        app = create_app(_real_settings())
        assert app is not None

    def test_settings_from_env_reads_new_vars(self, monkeypatch):
        monkeypatch.setenv("RETRIEVAL_URL", "http://r:8004/")
        monkeypatch.setenv("DOCUMENT_API_URL", "http://d:8001")
        monkeypatch.setenv("INTERNAL_SECRET", "s3cret")
        monkeypatch.setenv("MCP_DOWNSTREAM_TIMEOUT_SECONDS", "12")
        monkeypatch.delenv("DRUID_MCP_DEV_FAKE_CLIENTS", raising=False)
        settings = Settings.from_env()
        assert settings.retrieval_url == "http://r:8004"  # trailing slash stripped
        assert settings.document_api_url == "http://d:8001"
        assert settings.internal_secret == "s3cret"
        assert settings.downstream_timeout_seconds == 12.0
        assert settings.dev_fake_clients is False
