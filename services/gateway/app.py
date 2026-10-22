"""FastAPI gateway application (spec §4).

Request lifecycle for every protected route:

1. **Strip** spoofable identity headers (middleware) — a client can never
   smuggle ``X-Group-Id`` / ``X-User-Id`` / ``X-Druid-*`` past this point.
2. **Authenticate** — the ``Authorization: Bearer`` JWT is validated for
   signature, issuer, audience, and expiry. The trusted
   :class:`RequestContext` is built from token claims only.
3. **Quota** — per-tenant ingestion / query quotas; exhaustion → 429 with
   ``Retry-After``.
4. **Forward** — the request goes to the internal service with the
   HMAC-signed identity headers over the internal connection.
"""

from __future__ import annotations

import logging
import uuid
import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from services.catalog.identity import IdentityDenied, resolve, provision_first_admin

from services.common.request_context import RequestContext

from . import proxy as proxy_module
from .auth import AuthError, JWTSettings, validate_token
from .druid_internal import SPOOFABLE_HEADERS
from .quotas import QuotaConfig, QuotaStore, quota_store_from_env

log = logging.getLogger(__name__)


class GatewaySettings(BaseSettings):
    """Gateway configuration.

    Environment variable names follow the shared ``.env.example`` at the
    build root (no ``DRUID_`` prefix): ``JWT_SECRET``, ``JWT_ISSUER``,
    ``JWT_AUDIENCE``, ``JWKS_URL``, ``QUOTA_*``, ``MAX_*``, ``REDIS_URL``.
    ``DOCUMENT_API_URL`` / ``MCP_SERVER_URL`` / ``INTERNAL_SECRET`` are new
    names the coordinator should add to the shared template.
    """

    model_config = SettingsConfigDict(extra="ignore")

    document_api_url: str = "http://document_api:8001"
    mcp_server_url: str = "http://mcp_server:8002"
    internal_secret: str = "change-me-in-production"
    redis_url: str | None = None
    forward_timeout_seconds: float = 30.0

    # Quotas (spec §4).
    quota_ingest_pages_per_day: int = 1000
    quota_query_qps: int = 10
    max_upload_mb: int = 200
    max_pages_per_document: int = 2000

    # JWT validation (spec §4 transport security).
    jwt_secret: str | None = None
    jwt_algorithms: str = "HS256"
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwks_url: str | None = None
    auth_mode: str = Field("legacy", validation_alias="DRUID_AUTH_MODE")
    database_url: str | None = None
    druid_bootstrap_admin_sub: str = ""

    def jwt_settings(self) -> JWTSettings:
        algorithms = tuple(
            a.strip().upper() for a in self.jwt_algorithms.split(",") if a.strip()
        )
        return JWTSettings(
            secret=self.jwt_secret,
            algorithms=algorithms or ("HS256",),
            issuer=self.jwt_issuer,
            audience=self.jwt_audience,
            jwks_url=self.jwks_url,
        )

    def quota_config(self) -> QuotaConfig:
        return QuotaConfig(
            ingest_pages_per_day=self.quota_ingest_pages_per_day,
            query_qps=self.quota_query_qps,
            max_upload_mb=self.max_upload_mb,
            max_pages_per_document=self.max_pages_per_document,
        )


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _quota_exceeded(retry_after: float | None) -> JSONResponse:
    headers = {}
    if retry_after is not None:
        # Retry-After takes whole seconds; always round up, minimum 1.
        headers["Retry-After"] = str(max(1, int(retry_after) + 1))
    return JSONResponse(
        status_code=429,
        content={"detail": "quota exhausted"},
        headers=headers,
    )


def create_app(settings: GatewaySettings | None = None) -> FastAPI:
    settings = settings or GatewaySettings()
    jwt_settings = settings.jwt_settings()
    quota_config = settings.quota_config()
    quota_store: QuotaStore = quota_store_from_env(quota_config)
    log.info(
        "starting gateway: document_api=%s mcp_server=%s quota_store=%s "
        "ingest_pages_per_day=%d query_qps=%d max_upload_mb=%d "
        "max_pages_per_document=%d forward_timeout_s=%.1f jwt_algs=%s",
        settings.document_api_url,
        settings.mcp_server_url,
        type(quota_store).__name__,
        quota_config.ingest_pages_per_day,
        quota_config.query_qps,
        quota_config.max_upload_mb,
        quota_config.max_pages_per_document,
        settings.forward_timeout_seconds,
        ",".join(jwt_settings.algorithms),
    )

    app = FastAPI(title="new_DRUID gateway", version="0.1.0")
    from services.common.upload_limits import UploadBodyLimit
    app.add_middleware(UploadBodyLimit, max_bytes=(settings.max_upload_mb + 1) * 1024 * 1024,
                       concurrency=int(os.environ.get('UPLOAD_CONCURRENCY', '4')))

    app.state.settings = settings
    app.state.quota_store = quota_store
    identity_sessions = None
    if settings.auth_mode == "keycloak":
        if (settings.jwt_algorithms != "RS256" or not settings.jwt_issuer
                or not settings.jwt_audience or not settings.jwks_url
                or not settings.database_url):
            raise RuntimeError(
                "Keycloak mode requires RS256, JWT_ISSUER, JWT_AUDIENCE, "
                "JWKS_URL and DATABASE_URL"
            )
        if (not settings.internal_secret or len(settings.internal_secret) < 32):
            raise RuntimeError("Keycloak gateway mode requires a strong INTERNAL_SECRET")
        identity_sessions = sessionmaker(
            bind=create_engine(settings.database_url, pool_pre_ping=True),
            expire_on_commit=False,
        )
    elif settings.auth_mode != "legacy":
        raise RuntimeError("unknown DRUID_AUTH_MODE")

    # -- 1. strip spoofable identity headers from every inbound request ------
    @app.middleware("http")
    async def _strip_spoofable_headers(request: Request, call_next):
        raw = [(k, v) for k, v in request.scope["headers"]]
        cleaned = [
            (k, v)
            for (k, v) in raw
            if k.decode("latin-1").lower() not in SPOOFABLE_HEADERS
            and not k.decode("latin-1").lower().startswith("x-druid-")
        ]
        request.scope["headers"] = cleaned
        return await call_next(request)

    # -- 2. build the trusted context from the bearer token only ------------
    async def get_request_context(request: Request) -> RequestContext:
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise _AuthHTTPError("missing bearer token")
        try:
            claims = validate_token(
                token.strip(), jwt_settings,
                require_group=settings.auth_mode != "keycloak",
            )
        except AuthError as exc:
            raise _AuthHTTPError(str(exc)) from exc
        group = claims["group_id"]
        roles = claims["roles"]
        if identity_sessions is not None:
            selected = request.headers.get("x-group-selection") or None
            if selected is not None and len(selected) > 255:
                raise _AuthHTTPError("invalid group selection")
            try:
                with identity_sessions.begin() as session:
                    try:
                        actor = resolve(session, claims["user_id"], selected)
                    except IdentityDenied:
                        # Only explicitly configured initial administrator can
                        # seed DB identity, and only while no admin exists.
                        if settings.druid_bootstrap_admin_sub != claims["user_id"]:
                            raise
                        provision_first_admin(session, claims["user_id"],
                                              settings.druid_bootstrap_admin_sub)
                        actor = resolve(session, claims["user_id"], selected)
                    group, roles = actor.group_id, actor.roles
            except IdentityDenied as exc:
                raise _AuthHTTPError(str(exc)) from exc
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        return RequestContext(
            user_id=claims["user_id"], group_id=group,
            roles=roles, request_id=request_id,
        )

    @app.exception_handler(_AuthHTTPError)
    async def _auth_error_handler(request: Request, exc: "_AuthHTTPError"):
        log.warning(
            "auth failed → 401: %s request_id=%s path=%s",
            exc,
            _req_id(request),
            request.url.path,
        )
        return _unauthorized(str(exc))

    # -- public -------------------------------------------------------------
    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    # -- 3+4. quota, then forward -------------------------------------------
    @app.post("/v1/documents/upload")
    async def upload_document(
        request: Request, context: RequestContext = Depends(get_request_context)
    ):
        max_bytes = quota_config.max_upload_mb * 1024 * 1024
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > max_bytes:
            log.warning(
                "upload rejected → 413: content_length=%s > max=%d request_id=%s",
                content_length,
                max_bytes,
                context.request_id,
            )
            return JSONResponse(
                status_code=413,
                content={
                    "detail": f"upload exceeds {quota_config.max_upload_mb} MB limit"
                },
            )
        # Actual page admission is transactional in document_api after validation.
        decision = quota_store.check_query(context.group_id)
        if not decision.allowed:
            return _quota_exceeded(decision.retry_after_seconds)
        return await proxy_module.forward_request(
            request,
            context,
            settings.document_api_url,
            internal_secret=settings.internal_secret,
            timeout_seconds=settings.forward_timeout_seconds,
        )

    @app.post("/mcp")
    async def mcp_entrypoint(
        request: Request, context: RequestContext = Depends(get_request_context)
    ):
        """Route MCP calls with quota class derived from the tool name.

        ``ingest_document`` consumes ingestion quota (page_count from the
        tool arguments); everything else consumes the query QPS quota.
        Tool arguments never influence identity — only the quota class.
        """
        try:
            body = await request.json()
        except Exception:
            log.warning(
                "mcp rejected → 400: invalid JSON request_id=%s",
                context.request_id,
            )
            return JSONResponse(status_code=400, content={"detail": "invalid JSON"})
        quota_kind = "query"
        decision = quota_store.check_query(context.group_id)
        if not decision.allowed:
            _log_quota_rejection(request, context, quota_kind, decision)
            return _quota_exceeded(decision.retry_after_seconds)
        return await proxy_module.forward_request(
            request,
            context,
            settings.mcp_server_url,
            internal_secret=settings.internal_secret,
            timeout_seconds=settings.forward_timeout_seconds,
        )

    @app.api_route(
        "/v1/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def api_proxy(
        path: str, request: Request, context: RequestContext = Depends(get_request_context)
    ):
        # Reads and metadata writes consume the query quota; document
        # ingestion goes through /v1/documents/upload instead.
        decision = quota_store.check_query(context.group_id)
        if not decision.allowed:
            _log_quota_rejection(request, context, "query", decision)
            return _quota_exceeded(decision.retry_after_seconds)
        return await proxy_module.forward_request(
            request,
            context,
            settings.document_api_url,
            internal_secret=settings.internal_secret,
            timeout_seconds=settings.forward_timeout_seconds,
        )

    return app


def _classify_mcp_call(body: object) -> tuple[str, int]:
    """Return (quota_kind, pages) for a JSON-RPC MCP body.

    Only the *quota class* is derived from the body — never identity.
    Unknown shapes default to the query quota (fail toward the cheaper,
    read-only bucket).
    """
    if not isinstance(body, dict):
        return ("query", 0)
    if body.get("method") == "tools/call":
        params = body.get("params") or {}
        if isinstance(params, dict) and params.get("name") == "ingest_document":
            args = params.get("arguments") or {}
            try:
                pages = max(1, int(args.get("page_count", 1)))
            except (TypeError, ValueError):
                pages = 1
            return ("ingestion", pages)
    return ("query", 0)


class _AuthHTTPError(Exception):
    """Internal control-flow exception mapped to a 401 response."""


def _req_id(request: Request) -> str:
    return request.headers.get("x-request-id", "") or "-"


def _log_quota_rejection(
    request: Request, context: RequestContext, kind: str,
    decision,
) -> None:
    log.info(
        "quota exhausted → 429: group=%s kind=%s scope=%s remaining=%d "
        "request_id=%s path=%s",
        context.group_id,
        kind,
        decision.scope,
        decision.remaining,
        context.request_id,
        request.url.path,
    )


# The default application instance (uvicorn target: services.gateway.app:app).
app = create_app()
