"""Keycloak OpenID Connect Authorization Code + PKCE browser login.

The user authenticates at Keycloak. DRUID stores the verified access token
in a secure HttpOnly cookie and authorizes groups only through PostgreSQL.
No local usernames/passwords and no Keycloak-group-to-DRUID-role mapping.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from auth import JWTSettings, validate_token, AuthError
from .auth_pages import _safe_next

router = APIRouter()
_FLOW_COOKIE = "druid_oidc_flow"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _settings(request: Request):
    cfg = request.app.state.web_ui_settings
    if cfg.druid_auth_mode != "keycloak":
        raise HTTPException(status_code=404)
    if not (cfg.oidc_issuer and cfg.oidc_client_id and cfg.oidc_redirect_uri
            and cfg.oidc_session_secret and cfg.jwks_url
            and len(cfg.oidc_session_secret) >= 32
            and cfg.oidc_session_secret.lower() not in ("change-me", "changeme")):
        raise HTTPException(status_code=503, detail="OIDC is not configured")
    for url in (cfg.oidc_issuer, cfg.oidc_redirect_uri):
        if not url.startswith("https://") and not cfg.oidc_allow_insecure_http:
            raise HTTPException(status_code=503, detail="OIDC must use HTTPS")
    return cfg


async def _metadata(cfg):
    uri = cfg.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
        response = await client.get(uri)
        response.raise_for_status()
        doc = response.json()
    if doc.get("issuer") != cfg.oidc_issuer.rstrip("/"):
        raise HTTPException(status_code=502, detail="OIDC issuer mismatch")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not doc.get(key) or (not doc[key].startswith("https://")
                                and not cfg.oidc_allow_insecure_http):
            raise HTTPException(status_code=502, detail="unsafe OIDC provider metadata")
    if doc["jwks_uri"] != cfg.jwks_url:
        raise HTTPException(status_code=502, detail="unexpected OIDC signing keys URL")
    return doc


@router.get("/auth/oidc/start")
async def start(request: Request, next: str = "/documents"):
    cfg = _settings(request)
    provider = await _metadata(cfg)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    flow = jwt.encode({
        "state": state, "nonce": nonce, "verifier": verifier,
        "next": _safe_next(next), "exp": __import__("time").time() + 300,
    }, cfg.oidc_session_secret, algorithm="HS256")
    params = {
        "response_type": "code", "client_id": cfg.oidc_client_id,
        "scope": "openid profile email",
        "redirect_uri": cfg.oidc_redirect_uri, "state": state,
        "nonce": nonce, "code_challenge_method": "S256",
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
    }
    resp = RedirectResponse(provider["authorization_endpoint"] + "?" + urlencode(params), status_code=302)
    resp.set_cookie(_FLOW_COOKIE, flow, max_age=300, httponly=True,
                    secure=cfg.cookie_secure, samesite="Lax")
    return resp


@router.get("/auth/oidc/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    cfg = _settings(request)
    value = request.cookies.get(_FLOW_COOKIE, "")
    try:
        data = jwt.decode(value, cfg.oidc_session_secret,
                          algorithms=["HS256"], options={"require": ["exp", "state", "nonce", "verifier"]})
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="OIDC login expired")
    if not code or not state or not hmac.compare_digest(state, data["state"]):
        raise HTTPException(status_code=401, detail="OIDC state mismatch")
    provider = await _metadata(cfg)
    token_fields = {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": cfg.oidc_redirect_uri, "client_id": cfg.oidc_client_id,
        "code_verifier": data["verifier"],
    }
    if cfg.oidc_client_secret:
        token_fields["client_secret"] = cfg.oidc_client_secret
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        response = await client.post(provider["token_endpoint"], data=token_fields)
        response.raise_for_status()
        tokens = response.json()
    id_token = tokens.get("id_token", "")
    access_token = tokens.get("access_token", "")
    try:
        id_cfg = JWTSettings(secret=None, algorithms=("RS256",),
                             issuer=cfg.oidc_issuer.rstrip("/"),
                             audience=cfg.oidc_client_id, jwks_url=cfg.jwks_url)
        validate_token(id_token, id_cfg, require_group=False)
        claims = jwt.decode(id_token, options={"verify_signature": False})
        if not hmac.compare_digest(str(claims.get("nonce", "")), data["nonce"]):
            raise AuthError("OIDC nonce mismatch")
        validated = validate_token(access_token, request.app.state.jwt_settings,
                                   require_group=False)
        if validated["user_id"] != claims["sub"]:
            raise AuthError("OIDC subject mismatch")
    except (AuthError, jwt.PyJWTError):
        raise HTTPException(status_code=401, detail="OIDC token validation failed")
    resp = RedirectResponse(_safe_next(data.get("next", "/documents")), status_code=303)
    resp.delete_cookie(_FLOW_COOKIE)
    resp.set_cookie(cfg.cookie_name, access_token, max_age=min(
        cfg.cookie_max_age_seconds, max(1, int(tokens.get("expires_in", 300)))
    ), httponly=True, secure=cfg.cookie_secure, samesite=cfg.cookie_samesite)
    return resp
