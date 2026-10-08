"""Gateway → MCP seam: identity must survive the hop intact.

Simulates what happens in compose when the gateway forwards an MCP request
to the mcp_server service: the gateway validates the bearer token, derives
the trusted context from token claims ONLY, strips spoofable headers, and
forwards HMAC-signed X-Druid-* identity headers. The upstream is faked at
the httpx layer; the test asserts on what the gateway *would* put on the
wire, then verifies the signature with the same verifier backend services
use (services.gateway.druid_internal.verify_internal_headers).
"""

import time

import httpx
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

import services.gateway.proxy as proxy_module
from services.gateway.app import GatewaySettings, create_app
from services.gateway.auth import AuthError
from services.gateway.druid_internal import verify_internal_headers

SECRET = "test-secret"
ISSUER = "druid-test"
AUDIENCE = "druid-api"
INTERNAL_SECRET = "internal-test-secret"


def make_token(sub="user-1", group_id="tenant-a", roles=("reader",)):
    now = int(time.time())
    return pyjwt.encode(
        {
            "sub": sub,
            "group_id": group_id,
            "roles": list(roles),
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + 3600,
        },
        SECRET,
        algorithm="HS256",
    )


class FakeAsyncClient:
    """Captures the forwarded request; returns a canned MCP response."""

    last_request: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, headers=None, content=None):
        FakeAsyncClient.last_request = {
            "method": method,
            "url": url,
            "headers": dict(headers or {}),
            "content": content,
        }
        req = httpx.Request(method, url)
        return httpx.Response(
            200,
            json={"result": {"tools": ["search_knowledge_base"]}},
            request=req,
            headers={"content-type": "application/json"},
        )


@pytest.fixture()
def gateway(monkeypatch):
    monkeypatch.setattr(proxy_module.httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.last_request = None
    settings = GatewaySettings(
        jwt_secret=SECRET,
        jwt_issuer=ISSUER,
        jwt_audience=AUDIENCE,
        internal_secret=INTERNAL_SECRET,
        mcp_server_url="http://mcp_server:8002",
        document_api_url="http://document_api:8001",
    )
    return TestClient(create_app(settings), raise_server_exceptions=False)


def auth_headers(extra=None):
    headers = {"Authorization": f"Bearer {make_token()}"}
    headers.update(extra or {})
    return headers


def test_mcp_forward_carries_signed_identity(gateway):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search_knowledge_base",
            # A malicious client claims a different tenant in tool args.
            "arguments": {"query": "invoices", "group_id": "tenant-evil"},
        },
    }
    resp = gateway.post(
        "/mcp",
        json=body,
        headers=auth_headers(extra={
            # ... and tries to spoof identity headers directly.
            "X-Druid-Group-Id": "tenant-evil",
            "X-Druid-User-Id": "attacker",
        }),
    )
    assert resp.status_code == 200, resp.text
    fwd = FakeAsyncClient.last_request
    assert fwd is not None
    assert fwd["url"].startswith("http://mcp_server:8002/mcp")

    headers = fwd["headers"]
    # Identity comes from the token, never from args or spoofed headers.
    assert headers["X-Druid-Group-Id"] == "tenant-a"
    assert headers["X-Druid-User-Id"] == "user-1"
    assert "tenant-evil" not in headers["X-Druid-Group-Id"]
    assert "attacker" not in headers["X-Druid-User-Id"]

    # The signature verifies with the backend-side verifier.
    ctx = verify_internal_headers(headers, INTERNAL_SECRET)
    assert ctx.group_id == "tenant-a"
    assert ctx.user_id == "user-1"
    assert "tenant-evil" not in str(headers)


def test_mcp_forward_rejects_tampered_signature(gateway):
    resp = gateway.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    headers = dict(FakeAsyncClient.last_request["headers"])
    headers["X-Druid-Group-Id"] = "tenant-evil"  # tamper after signing
    with pytest.raises(AuthError):
        verify_internal_headers(headers, INTERNAL_SECRET)


def test_unauthenticated_mcp_request_is_401(gateway):
    resp = gateway.post("/mcp", json={"jsonrpc": "2.0", "id": 1})
    assert resp.status_code == 401
    assert FakeAsyncClient.last_request is None
