"""web_ui configuration (spec §16).

Environment variable names follow the shared ``.env.example``: ``JWT_*``
names are identical to the gateway's so one secret provisions both.
``GATEWAY_URL`` points at the gateway (the UI never talks to backend
services directly).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class WebUISettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    druid_auth_mode: str = "legacy"  # production: keycloak
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    oidc_session_secret: str = ""
    oidc_allow_insecure_http: bool = False

    # Downstream: everything goes through the gateway with the user's token.
    gateway_url: str = "http://gateway:8000"
    forward_timeout_seconds: float = 30.0

    # JWT validation — same contract as the gateway (§4). The UI validates
    # the token from its own cookie with these settings.
    jwt_secret: str | None = None
    jwt_algorithms: str = "HS256"
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwks_url: str | None = None

    # Session cookie carrying the validated JWT.
    cookie_name: str = "druid_at"
    cookie_secure: bool = True  # set False only for plain-HTTP local dev
    cookie_samesite: str = "Lax"
    cookie_max_age_seconds: int = 12 * 3600

    # Uploads (mirror the gateway/document_api limits).
    max_upload_mb: int = 200
    upload_chunk_mb: int = 8
    upload_session_ttl_seconds: int = 3600

    # Search page.
    search_default_top_k: int = 8


__all__ = ["WebUISettings"]
