"""Unit tests for the gateway (spec §4).

Run from the build root:  python -m pytest services/gateway/tests -q

The upstream services are faked: ``forward_request`` is monkeypatched with
an echo that returns the trusted context and the headers the gateway *would*
forward. This verifies auth, header stripping, tenant-ignoring, and quotas
without any backend running.
"""

import time

import jwt as pyjwt
import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import services.gateway.proxy as proxy_module
from services.common.request_context import RequestContext
from services.gateway import app as app_module
from services.gateway.app import GatewaySettings, create_app
from services.gateway.auth import AuthError, validate_token, settings_from_env
from services.gateway.druid_internal import (
    SPOOFABLE_HEADERS,
    build_internal_headers,
    strip_spoofable_headers,
    verify_internal_headers,
)

SECRET = "test-secret"
ISSUER = "druid-test"
AUDIENCE = "druid-api"
INTERNAL_SECRET = "internal-test-secret"


def make_token(
    sub="user-1",
    group_id="tenant-a",
    roles=("reader",),
    issuer=ISSUER,
    audience=AUDIENCE,
    exp_offset=3600,
    secret=SECRET,
    include_group_id=True,
):
    now = int(time.time())
    claims = {
        "sub": sub,
        "roles": list(roles),
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + exp_offset,
    }
    if include_group_id:
        claims["group_id"] = group_id
    return pyjwt.encode(claims, secret, algorithm="HS256")


def make_settings(**overrides):
    kwargs = dict(
        jwt_secret=SECRET,
        jwt_issuer=ISSUER,
        jwt_audience=AUDIENCE,
        internal_secret=INTERNAL_SECRET,
        document_api_url="http://document-api",
        mcp_server_url="http://mcp-server",
    )
    kwargs.update(overrides)
    return GatewaySettings(**kwargs)


async def fake_forward(request, context, upstream_base, **kwargs):
    return JSONResponse(
        {
            "upstream": upstream_base,
            "path": request.url.path,
            "context": {
                "user_id": context.user_id,
                "group_id": context.group_id,
                "roles": list(context.roles),
                "request_id": context.request_id,
            },
            # What the real forward would put on the wire:
            "internal_headers": build_internal_headers(context, INTERNAL_SECRET),
            "received_headers": dict(request.headers),
        }
    )


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(proxy_module, "forward_request", fake_forward)
    app = create_app(make_settings())
    return TestClient(app, raise_server_exceptions=False)


def auth_headers(token=None, **extra):
    headers = {"Authorization": f"Bearer {token or make_token()}"}
    headers.update(extra)
    return headers


# ---------------------------------------------------------------------------
# §4: trusted context from the token only
# ---------------------------------------------------------------------------

def test_valid_token_builds_trusted_context(client):
    resp = client.get("/v1/documents/abc", headers=auth_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["context"]["user_id"] == "user-1"
    assert body["context"]["group_id"] == "tenant-a"
    assert body["context"]["roles"] == ["reader"]
    assert body["upstream"] == "http://document-api"
    internal = body["internal_headers"]
    assert internal["X-Druid-User-Id"] == "user-1"
    assert internal["X-Druid-Group-Id"] == "tenant-a"


def test_spoofed_identity_headers_are_stripped_and_ignored(client):
    token = make_token(sub="bob", group_id="tenant-a", roles=("reader",))
    resp = client.get(
        "/v1/documents/abc",
        headers=auth_headers(
            token,
            **{
                "X-Group-Id": "evil-tenant",
                "X-Tenant-Id": "evil-tenant",
                "X-User-Id": "mallory",
                "X-Roles": "admin",
                "X-Forwarded-User": "mallory",
                "X-Druid-Group-Id": "evil-tenant",
                "X-Druid-Internal-Sig": "v1:forged",
            },
        ),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Identity comes from the token alone.
    assert body["context"]["user_id"] == "bob"
    assert body["context"]["group_id"] == "tenant-a"
    assert body["context"]["roles"] == ["reader"]
    # And none of the spoofed headers reached the (fake) upstream.
    received = {k.lower(): v for k, v in body["received_headers"].items()}
    for spoofed in SPOOFABLE_HEADERS:
        assert spoofed not in received, f"{spoofed} leaked upstream"


def test_caller_supplied_tenant_id_in_tool_args_is_ignored(client):
    """MCP tool arguments must never influence tenant identity (§4)."""
    token = make_token(group_id="tenant-a")
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "ingest_document",
            "arguments": {"group_id": "evil-tenant", "page_count": 3},
        },
    }
    resp = client.post("/mcp", json=body, headers=auth_headers(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["context"]["group_id"] == "tenant-a"
    # Quota was consumed against the token's tenant, not the spoofed one.
    store = client.app.state.quota_store
    decision = store.consume_ingestion_pages("tenant-a", 998)
    assert decision.allowed is False  # 3 + 998 > 1000
    assert store.consume_ingestion_pages("evil-tenant", 1000).allowed is True


def test_strip_spoofable_headers_unit():
    headers = {"X-Group-Id": "evil", "Content-Type": "application/json",
               "X-DRUID-ROLES": "admin"}
    removed = strip_spoofable_headers(headers)
    assert removed == 2
    assert headers == {"Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# fail closed: missing / invalid tokens
# ---------------------------------------------------------------------------

def test_missing_token_is_401(client):
    resp = client.get("/v1/documents/abc")
    assert resp.status_code == 401


def test_malformed_authorization_is_401(client):
    resp = client.get("/v1/documents/abc", headers={"Authorization": "Token xyz"})
    assert resp.status_code == 401


def test_expired_token_is_401(client):
    resp = client.get("/v1/documents/abc",
                      headers=auth_headers(make_token(exp_offset=-60)))
    assert resp.status_code == 401


def test_wrong_audience_is_401(client):
    resp = client.get("/v1/documents/abc",
                      headers=auth_headers(make_token(audience="someone-else")))
    assert resp.status_code == 401


def test_wrong_issuer_is_401(client):
    resp = client.get("/v1/documents/abc",
                      headers=auth_headers(make_token(issuer="evil-issuer")))
    assert resp.status_code == 401


def test_bad_signature_is_401(client):
    resp = client.get("/v1/documents/abc",
                      headers=auth_headers(make_token(secret="wrong-secret")))
    assert resp.status_code == 401


def test_missing_group_id_claim_is_401(client):
    resp = client.get("/v1/documents/abc",
                      headers=auth_headers(make_token(include_group_id=False)))
    assert resp.status_code == 401


def test_validate_token_unit_rejects_garbage():
    settings = settings_from_env(
        {
            "JWT_SECRET": SECRET,
            "JWT_ISSUER": ISSUER,
            "JWT_AUDIENCE": AUDIENCE,
        }
    )
    with pytest.raises(AuthError):
        validate_token("not-a-jwt", settings)
    with pytest.raises(AuthError):
        validate_token("", settings)


# ---------------------------------------------------------------------------
# §4: quotas → 429 with Retry-After
# ---------------------------------------------------------------------------

def test_query_qps_quota_returns_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(proxy_module, "forward_request", fake_forward)
    app = create_app(make_settings(quota_query_qps=2))
    c = TestClient(app, raise_server_exceptions=False)
    headers = auth_headers()
    assert c.get("/v1/documents/a", headers=headers).status_code == 200
    assert c.get("/v1/documents/b", headers=headers).status_code == 200
    resp = c.get("/v1/documents/c", headers=headers)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) >= 1


def test_ingestion_daily_quota_returns_429(monkeypatch):
    monkeypatch.setattr(proxy_module, "forward_request", fake_forward)
    app = create_app(make_settings(quota_ingest_pages_per_day=10))
    c = TestClient(app, raise_server_exceptions=False)
    headers = auth_headers()

    def ingest(pages):
        return c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "ingest_document",
                           "arguments": {"page_count": pages}},
            },
            headers=headers,
        )

    assert ingest(6).status_code == 200
    resp = ingest(5)  # 6 + 5 > 10
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_upload_enforces_size_and_page_limits(monkeypatch):
    monkeypatch.setattr(proxy_module, "forward_request", fake_forward)
    app = create_app(make_settings(max_upload_mb=1,
                                   max_pages_per_document=2000))
    c = TestClient(app, raise_server_exceptions=False)
    headers = auth_headers()

    # Over the 1 MB cap -> 413.
    resp = c.post("/v1/documents/upload", content=b"x" * (2 * 1024 * 1024),
                  headers=headers)
    assert resp.status_code == 413

    # Over the per-document page cap -> 400.
    resp = c.post("/v1/documents/upload?page_count=2001", content=b"x",
                  headers=headers)
    assert resp.status_code == 400

    # Within limits -> forwarded.
    resp = c.post("/v1/documents/upload?page_count=5", content=b"x",
                  headers=headers)
    assert resp.status_code == 200
    assert resp.json()["upstream"] == "http://document-api"


def test_search_tool_uses_query_quota_not_ingestion(monkeypatch):
    monkeypatch.setattr(proxy_module, "forward_request", fake_forward)
    app = create_app(make_settings(quota_query_qps=1,
                                   quota_ingest_pages_per_day=1000))
    c = TestClient(app, raise_server_exceptions=False)
    headers = auth_headers()
    search_body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_knowledge_base",
                   "arguments": {"query": "hello"}},
    }
    assert c.post("/mcp", json=search_body, headers=headers).status_code == 200
    resp = c.post("/mcp", json=search_body, headers=headers)
    assert resp.status_code == 429  # query quota, not ingestion quota


# ---------------------------------------------------------------------------
# internal identity propagation
# ---------------------------------------------------------------------------

def test_internal_header_sign_verify_roundtrip():
    context = RequestContext(user_id="u1", group_id="g1",
                            roles=("reader", "reviewer"), request_id="r1")
    headers = build_internal_headers(context, INTERNAL_SECRET)
    recovered = verify_internal_headers(headers, INTERNAL_SECRET)
    assert recovered == context


def test_internal_header_tampering_fails_closed():
    context = RequestContext(user_id="u1", group_id="g1", request_id="r1")
    headers = build_internal_headers(context, INTERNAL_SECRET)
    headers["X-Druid-Group-Id"] = "evil-tenant"  # tamper after signing
    with pytest.raises(AuthError):
        verify_internal_headers(headers, INTERNAL_SECRET)
    with pytest.raises(AuthError):
        verify_internal_headers(headers, "wrong-secret")


def test_healthz_needs_no_auth(client):
    assert client.get("/healthz").status_code == 200
