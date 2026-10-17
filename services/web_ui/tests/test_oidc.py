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
