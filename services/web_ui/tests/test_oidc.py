"""Keycloak SSO: PKCE, CSRF state, and no pasted-token bypass."""
import hashlib
import base64
from urllib.parse import parse_qs, urlsplit

import jwt
from fastapi.testclient import TestClient

from main import create_app
from settings import WebUISettings
from routes import oidc_pages


def _app():
    return create_app(WebUISettings(
        druid_auth_mode="keycloak",
        oidc_issuer="https://login.example.test/realms/druid",
        oidc_client_id="druid-web",
        oidc_redirect_uri="https://druid.example.test/auth/oidc/callback",
        oidc_session_secret="test-only-very-long-flow-secret-32characters",
        jwks_url="https://login.example.test/realms/druid/protocol/openid-connect/certs",
        jwt_issuer="https://login.example.test/realms/druid",
        jwt_audience="druid-api",
        jwt_algorithms="RS256",
        cookie_secure=False,
    ))


async def _provider(config):
    return {
        "issuer": config.oidc_issuer,
        "authorization_endpoint": "https://login.example.test/authorize",
        "token_endpoint": "https://login.example.test/token",
        "jwks_uri": config.jwks_url,
    }


def test_oidc_start_uses_state_nonce_pkce_and_verified_redirect(monkeypatch):
    monkeypatch.setattr(oidc_pages, "_metadata", _provider)
    with TestClient(_app()) as client:
        first = client.get("/login", follow_redirects=False)
        assert first.status_code == 303
        assert "/auth/oidc/start" in first.headers["location"]
        second = client.get("/auth/oidc/start?next=%2Fdocuments", follow_redirects=False)
        assert second.status_code == 302
        qs = parse_qs(urlsplit(second.headers["location"]).query)
        assert qs["response_type"] == ["code"]
        assert qs["code_challenge_method"] == ["S256"]
        assert qs["scope"] == ["openid profile email"]
        cookie = client.cookies.get("druid_oidc_flow")
        decoded = jwt.decode(
            cookie, client.app.state.web_ui_settings.oidc_session_secret,
            algorithms=["HS256"],
        )
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(decoded["verifier"].encode()).digest()
        ).rstrip(b"=").decode()
        assert expected == qs["code_challenge"][0]
        assert decoded["state"] == qs["state"][0]
        assert decoded["nonce"]
        failed = client.get(
            "/auth/oidc/callback?code=ignored&state=forged", follow_redirects=False
        )
        assert failed.status_code == 401


def test_no_legacy_jwt_paste_path_in_keycloak_mode():
    with TestClient(_app()) as client:
        response = client.post("/login", data={"token": "bogus", "next": "/documents"})
        assert response.status_code == 403


def test_druid_roles_are_not_taken_from_keycloak_claims(monkeypatch):
    from deps import get_identity
    monkeypatch.setattr("deps.validate_token", lambda *args, **kwargs: {
        "user_id": "keycloak-sub", "group_id": "", "roles": ("admin", "reviewer")
    })
    app = _app()

    @app.get("/identity-for-test")
    def identity_route(request: __import__("fastapi").Request):
        identity = get_identity(request, request.app.state.jwt_settings)
        return {"roles": identity.roles, "group_id": identity.group_id}

    with TestClient(app) as client:
        client.cookies.set("druid_at", "signed-access-token")
        client.cookies.set("druid_selected_group", "engineering")
        response = client.get("/identity-for-test")
        assert response.status_code == 200
        assert response.json() == {
            "roles": ["user"], "group_id": "engineering",
        }



def test_complete_oidc_callback_accepts_only_verified_keycloak_tokens(monkeypatch):
    import json
    import time
    import httpx
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    import auth

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public_jwk["kid"] = "test-rsa-key"

    class Response:
        def __init__(self, body):
            self.body = body
        def raise_for_status(self):
            pass
        def json(self):
            return self.body

    original_get = httpx.get
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: Response(
        {"keys": [public_jwk]}
    ))
    auth._jwks_cache.clear()
    monkeypatch.setattr(oidc_pages, "_metadata", _provider)
    with TestClient(_app()) as client:
        start = client.get("/auth/oidc/start", follow_redirects=False)
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        data = jwt.decode(client.cookies.get("druid_oidc_flow"),
                          client.app.state.web_ui_settings.oidc_session_secret,
                          algorithms=["HS256"])
        base = {"iss": "https://login.example.test/realms/druid", "sub": "subject-123",
                "exp": int(time.time()) + 300, "iat": int(time.time())}
        id_token = jwt.encode(
            {**base, "aud": "druid-web", "nonce": data["nonce"]}, pem,
            algorithm="RS256", headers={"kid": "test-rsa-key"},
        )
        access_token = jwt.encode(
            {**base, "aud": "druid-api"}, pem,
            algorithm="RS256", headers={"kid": "test-rsa-key"},
        )

        class TokenClient:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def post(self, url, data):
                assert data["code_verifier"] == jwt.decode(
                    client.cookies.get("druid_oidc_flow"),
                    client.app.state.web_ui_settings.oidc_session_secret,
                    algorithms=["HS256"]
                )["verifier"]
                return Response({"id_token": id_token, "access_token": access_token,
                                 "expires_in": 300})

        monkeypatch.setattr(oidc_pages.httpx, "AsyncClient", TokenClient)
        resp = client.get("/auth/oidc/callback?code=one-time&state=" + state,
                          follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/documents"
        assert client.cookies.get("druid_at") == access_token
        assert not client.cookies.get("druid_oidc_flow")
