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

import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.common.request_context import RequestContext

from . import proxy as proxy_module
from .auth import AuthError, JWTSettings, validate_token
from .druid_internal import SPOOFABLE_HEADERS
from .quotas import QuotaConfig, QuotaStore, quota_store_from_env


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

    app = FastAPI(title="new_DRUID gateway", version="0.1.0")
    app.state.settings = settings
    app.state.quota_store = quota_store

    # -- 1. strip spoofable identity headers from every inbound request ------
    @app.middleware("http")
    async def _strip_spoofable_headers(request: Request, call_next):
        raw = [(k, v) for k, v in request.scope["headers"]]
        cleaned = [
            (k, v) for (k, v) in raw if k.decode("latin-1").lower() not in SPOOFABLE_HEADERS
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
            claims = validate_token(token.strip(), jwt_settings)
        except AuthError as exc:
            raise _AuthHTTPError(str(exc)) from exc
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        return RequestContext(
            user_id=claims["user_id"],
            group_id=claims["group_id"],
            roles=claims["roles"],
            request_id=request_id,
        )

    @app.exception_handler(_AuthHTTPError)
    async def _auth_error_handler(request: Request, exc: "_AuthHTTPError"):
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
            return JSONResponse(
                status_code=413,
                content={
                    "detail": f"upload exceeds {quota_config.max_upload_mb} MB limit"
                },
            )
        # page_count may arrive as a form field or query param; it only
        # sizes the quota reservation, never identity.
        page_count = 1
        raw_page_count = request.query_params.get("page_count")
        if raw_page_count is None and request.headers.get(
            "content-type", ""
        ).startswith("multipart/form-data"):
            form = await request.form()
            raw_page_count = form.get("page_count")
        if raw_page_count is not None:
            try:
                page_count = int(raw_page_count)
            except (TypeError, ValueError):
                return JSONResponse(
                    status_code=400, content={"detail": "page_count must be an integer"}
                )
        if page_count < 1 or page_count > quota_config.max_pages_per_document:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "page_count must be between 1 and "
                    f"{quota_config.max_pages_per_document}"
                },
            )
        decision = quota_store.consume_ingestion_pages(context.group_id, page_count)
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
            return JSONResponse(status_code=400, content={"detail": "invalid JSON"})
        quota_kind, pages = _classify_mcp_call(body)
        if quota_kind == "ingestion":
            decision = quota_store.consume_ingestion_pages(context.group_id, pages)
        else:
            decision = quota_store.check_query(context.group_id)
        if not decision.allowed:
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


# The default application instance (uvicorn target: services.gateway.app:app).
app = create_app()
