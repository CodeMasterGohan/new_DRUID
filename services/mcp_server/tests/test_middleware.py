"""Transport-security tests for the Streamable HTTP middleware (§11).

- Missing/malformed Authorization -> 401 (fail closed).
- Invalid token -> 401.
- Valid token -> 200 with a validated context installed.
- Non-allowlisted Origin -> 403; absent Origin -> allowed.
- Spoofable identity headers are stripped before reaching the app.
- The MCP session ID is never consulted for identity.
"""

from __future__ import annotations

import httpx
import pytest

from mcp_server.auth import JWTVerifier
from mcp_server.context import get_context
from mcp_server.server import AuthMiddleware

from .conftest import AUDIENCE, ISSUER, SECRET
from .test_auth import mint


async def dummy_app(scope, receive, send):
    assert scope["type"] == "http"
    # Identity must be available ONLY via the validated context...
    ctx = get_context()
    # ...and spoofable headers must be gone.
    headers = {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in scope["headers"]
    }
    assert "x-user-id" not in headers
    assert "x-group-id" not in headers
    body = f"ok group={ctx.group_id} user={ctx.user_id}".encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def make_client(allowed_origins=()) -> httpx.AsyncClient:
    verifier = JWTVerifier(
        key=SECRET, algorithms=("HS256",), issuer=ISSUER, audience=AUDIENCE
    )
    app = AuthMiddleware(dummy_app, verifier=verifier, allowed_origins=allowed_origins)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app))


async def test_missing_auth_header_is_401():
    async with make_client() as client:
        resp = await client.post("http://test/mcp")
    assert resp.status_code == 401


async def test_malformed_auth_header_is_401():
    async with make_client() as client:
        resp = await client.post("http://test/mcp", headers={"Authorization": "Token abc"})
    assert resp.status_code == 401


async def test_invalid_token_is_401():
    async with make_client() as client:
        resp = await client.post(
            "http://test/mcp", headers={"Authorization": "Bearer not-a-jwt"}
        )
    assert resp.status_code == 401


async def test_expired_token_is_401():
    token = mint(exp_offset=-60)
    async with make_client() as client:
        resp = await client.post(
            "http://test/mcp", headers={"Authorization": f"Bearer {token}"}
        )
    assert resp.status_code == 401


async def test_valid_token_reaches_app_with_context():
    token = mint()
    async with make_client() as client:
        resp = await client.post(
            "http://test/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "mcp-session-id": "attacker-chosen-session",
                "X-User-Id": "mallory",
                "X-Group-Id": "evil-corp",
            },
        )
    # Spoofed headers stripped; session ID ignored; identity from the token.
    assert resp.status_code == 200
    assert resp.text == "ok group=acme user=u1"


async def test_disallowed_origin_is_403():
    token = mint()
    async with make_client(allowed_origins=("https://app.internal.example",)) as client:
        resp = await client.post(
            "http://test/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "https://evil.example",
            },
        )
    assert resp.status_code == 403


async def test_allowlisted_origin_passes():
    token = mint()
    async with make_client(allowed_origins=("https://app.internal.example",)) as client:
        resp = await client.post(
            "http://test/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "https://app.internal.example",
            },
        )
    assert resp.status_code == 200


async def test_absent_origin_allowed_for_native_clients():
    token = mint()
    async with make_client(allowed_origins=("https://app.internal.example",)) as client:
        resp = await client.post(
            "http://test/mcp", headers={"Authorization": f"Bearer {token}"}
        )
    assert resp.status_code == 200
