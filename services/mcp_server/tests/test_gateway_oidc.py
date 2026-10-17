"""In Keycloak mode the gateway, not MCP token claims, authorizes roles."""
import hashlib
import hmac

import httpx

from mcp_server.auth import JWTVerifier
from mcp_server.server import AuthMiddleware
from .conftest import AUDIENCE, ISSUER, SECRET
from .test_middleware import dummy_app


async def _call(*, roles="member", signature_valid=True, include_identity=True):
    secret = "internal-druid-identity-secret-for-contracts"
    user = "alice"
    group = "engineering"
    req_id = "id123"
    sig = "v1:" + hmac.new(secret.encode(),
        f"druid-v1|{user}|{group}|{roles}|{req_id}".encode(),
        hashlib.sha256).hexdigest()
    if not signature_valid:
        sig = "v1:" + "0" * 64
    verifier = JWTVerifier(key=SECRET, algorithms=("HS256",),
                           issuer=ISSUER, audience=AUDIENCE)
    app = AuthMiddleware(dummy_app, verifier=verifier, allowed_origins=(),
                         gateway_secret=secret)
    headers = {"Authorization": "Bearer opaque-keycloak-token"}
    if include_identity:
        headers.update({
            "X-Druid-User-Id": user, "X-Druid-Group-Id": group,
            "X-Druid-Roles": roles, "X-Druid-Request-Id": req_id,
            "X-Druid-Internal-Sig": sig,
        })
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        return await client.post("http://test/mcp", headers=headers)


async def test_signed_gateway_context_is_used_for_keycloak_mcp():
    r = await _call()
    assert r.status_code == 200
    assert r.text == "ok group=engineering user=alice"


async def test_unsigned_gateway_context_is_denied():
    r = await _call(include_identity=False)
    assert r.status_code == 401


async def test_tampered_gateway_context_is_denied():
    r = await _call(roles="admin", signature_valid=False)
    assert r.status_code == 401
