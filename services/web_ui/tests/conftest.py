"""Test fixtures: app with a mocked gateway transport.

The mock gateway simulates per-tenant downstream data so tests can prove
tenant isolation: the UI must forward the *caller's* token and never mix
tenants.
"""

from __future__ import annotations

import json
import time

import httpx
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from main import create_app
from settings import WebUISettings

TEST_SECRET = "test-secret-for-web-ui"
OTHER_SECRET = "wrong-secret"


def make_token(
    group_id: str,
    roles: tuple[str, ...] = ("user",),
    user_id: str = "u1",
    secret: str = TEST_SECRET,
    expired: bool = False,
) -> str:
    now = int(time.time())
    return pyjwt.encode(
        {
            "sub": user_id,
            "group_id": group_id,
            "roles": list(roles),
            "iss": "test-issuer",
            "aud": "test-audience",
            "iat": now - 10,
            "exp": now - 10 if expired else now + 3600,
        },
        secret,
        algorithm="HS256",
    )


def _token_group(request: httpx.Request) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        claims = pyjwt.decode(token, options={"verify_signature": False})
    except Exception:
        return None
    return claims.get("group_id")


def doc_summary(doc_id: str, name: str, group: str) -> dict:
    return {
        "document_id": doc_id,
        "display_name": name,
        "checksum_sha256": "ab" * 32,
        "active_version_id": f"{doc_id}-v1",
        "is_deleted": False,
        "created_at": "2026-10-09T00:00:00",
        "updated_at": "2026-10-09T00:00:00",
        "_group": group,
    }


def doc_detail(doc_id: str, name: str) -> dict:
    d = doc_summary(doc_id, name, "")
    d["versions"] = [
        {
            "version_id": f"{doc_id}-v1",
            "version_number": 1,
            "status": "READY",
            "parser": "native",
            "parser_revision": "r1",
            "page_count": 12,
            "chunk_count_actual": 40,
            "is_active": True,
            "created_at": "2026-10-09T00:00:00",
        }
    ]
    d["artifacts"] = {}
    return d


class MockGateway:
    """httpx.MockTransport handler with per-token tenant data."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.admin_enabled = False
        self.resolved: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        group = _token_group(request)
        if group is None:
            return httpx.Response(401, json={"detail": "unauthorized"})
        path = request.url.path
        method = request.method

        if path == "/v1/documents" and method == "GET":
            docs = [
                doc_summary(f"doc-{group}-1", f"report-{group}.pdf", group),
                doc_summary(f"doc-{group}-2", f"notes-{group}.pdf", group),
            ]
            return httpx.Response(
                200, json={"documents": docs, "limit": 100, "offset": 0}
            )

        if path.startswith("/v1/documents/") and path.endswith("/status"):
            doc_id = path.split("/")[3]
            return httpx.Response(
                200,
                json={
                    "job_id": f"job-{doc_id}",
                    "job_type": "ingestion",
                    "state": "READY",
                    "document_id": doc_id,
                    "document_version_id": f"{doc_id}-v1",
                    "attempt_count": 1,
                    "error": None,
                    "created_at": "2026-10-09T00:00:00",
                    "updated_at": "2026-10-09T00:00:00",
                },
            )

        if path.startswith("/v1/documents/") and method == "GET":
            doc_id = path.split("/")[3]
            if not doc_id.startswith(f"doc-{group}"):
                # Simulate downstream tenant scoping: not yours → 404.
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json=doc_detail(doc_id, f"report-{group}.pdf"))

        if path.startswith("/v1/documents/") and method == "DELETE":
            doc_id = path.split("/")[3]
            return httpx.Response(
                202,
                json={
                    "document_id": doc_id,
                    "job_id": f"del-{doc_id}",
                    "status": "DELETING",
                },
            )

        if path == "/v1/documents/upload" and method == "POST":
            return httpx.Response(
                201,
                json={
                    "document_id": "doc-new-1",
                    "job_id": "job-new-1",
                    "status": "QUEUED",
                },
            )

        if path == "/v1/review/items" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "item_id": "item-1",
                            "document_id": f"doc-{group}-1",
                            "document_version_id": f"doc-{group}-1-v1",
                            "page_index": 3,
                            "status": "pending",
                            "created_at": "2026-10-09T00:00:00",
                        }
                    ],
                    "limit": 100,
                    "offset": 0,
                },
            )

        if path.startswith("/v1/review/items/") and path.endswith("/resolve"):
            body = json.loads(request.content or b"{}")
            self.resolved.append({"group": group, "body": body})
            return httpx.Response(
                200,
                json={"item_id": "item-1", "status": body.get("decision"), "reviewed_by": "u1"},
            )

        if path.startswith("/v1/review/items/") and method == "GET":
            return httpx.Response(
                200,
                json={
                    "item_id": "item-1",
                    "document_id": f"doc-{group}-1",
                    "document_version_id": f"doc-{group}-1-v1",
                    "page_index": 3,
                    "status": "pending",
                    "page_render_key": "renders/x.png",
                    "page_render_url": "http://minio/renders/x.png?sig=1",
                    "parse_attempts": {"native": {"ok": False}},
                    "routing_diagnostics": {"signal": "table"},
                    "reviewer_decision": None,
                    "reviewed_by": None,
                    "reviewed_at": None,
                    "created_at": "2026-10-09T00:00:00",
                },
            )

        if path == "/mcp" and method == "POST":
            passages = [
                {
                    "provenance": {
                        "document_id": f"doc-{group}-1",
                        "filename": f"report-{group}.pdf",
                        "chunk_id": "c1",
                        "page_start": 3,
                        "page_end": 5,
                        "modality": "text",
                        "parser": "native-detected",
                        "source_reference": "s",
                        "page_image_ref": None,
                    },
                    "text": "The quick brown fox.",
                }
            ]
            mcp_text = json.dumps({"passages": passages})
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [{"type": "text", "text": mcp_text}]},
                },
            )

        if path.startswith("/v1/admin/"):
            if not self.admin_enabled:
                return httpx.Response(404, json={"detail": "not found"})
            if path == "/v1/admin/documents":
                docs = [doc_summary("doc-a-1", "a.pdf", "group-a")]
                return httpx.Response(
                    200, json={"documents": docs, "limit": 100, "offset": 0}
                )
            if path == "/v1/admin/jobs":
                return httpx.Response(
                    200,
                    json={
                        "jobs": [
                            {
                                "job_id": "job-1",
                                "group_id": "group-a",
                                "job_type": "ingestion",
                                "state": "FAILED",
                                "attempt_count": 3,
                                "error": "boom",
                                "updated_at": "2026-10-09T00:00:00",
                            }
                        ],
                        "limit": 100,
                        "offset": 0,
                    },
                )
            if path == "/v1/admin/quotas":
                return httpx.Response(
                    200,
                    json={
                        "tenants": [
                            {
                                "group_id": "group-a",
                                "ingest_pages_used": 42,
                                "ingest_pages_limit": 1000,
                                "query_qps": 2,
                                "query_qps_limit": 10,
                            }
                        ]
                    },
                )

        return httpx.Response(404, json={"detail": "not found"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def last_auth_header(self) -> str:
        return self.requests[-1].headers.get("authorization", "")


@pytest.fixture()
def mock_gateway() -> MockGateway:
    return MockGateway()


@pytest.fixture()
def ui_settings() -> WebUISettings:
    return WebUISettings(
        gateway_url="http://test-gateway",
        jwt_secret=TEST_SECRET,
        jwt_algorithms="HS256",
        jwt_issuer="test-issuer",
        jwt_audience="test-audience",
        cookie_secure=False,  # TestClient is plain HTTP
    )


@pytest.fixture()
def client(ui_settings, mock_gateway) -> TestClient:
    app = create_app(ui_settings, api_transport=mock_gateway.transport)
    return TestClient(app, raise_server_exceptions=True)


def login_as(client: TestClient, group_id: str, roles=("user",)) -> str:
    token = make_token(group_id, roles)
    resp = client.post("/login", data={"token": token, "next": "/documents"})
    # TestClient follows the 303 → /documents; the session cookie must exist.
    assert resp.status_code == 200, resp.text
    assert "druid_at" in client.cookies, "session cookie was not set"
    return token
