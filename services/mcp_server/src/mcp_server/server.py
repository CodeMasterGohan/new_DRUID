"""MCP server assembly: Streamable HTTP (authenticated) transport.

Transport security (§11):
- Every request must carry ``Authorization: Bearer <token>``. The token is
  validated (signature, issuer, audience, expiry); anything else -> 401.
- If an ``Origin`` header is present (browser-based clients), it must be in
  the configured allowlist — DNS-rebinding protection per the MCP transport
  specification. Absent Origin (native clients) is permitted.
- Spoofable identity headers (``X-User-Id``, ``X-Group-Id``, ...) are
  stripped: identity comes only from the validated token, never headers.
- MCP session IDs (``mcp-session-id``) are managed by the transport and are
  NEVER used as an authentication identity.
- A server-side request_id is generated per request for tracing.

The stdio adapter (``stdio_adapter.py``) is a SEPARATE entrypoint with its
own explicit trusted credential context. Gateway headers never apply to it.
"""

from __future__ import annotations

import logging
import hmac
import hashlib
import os
import time
from dataclasses import dataclass

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount

from .auth import AuthError, TokenVerifier, JWTVerifier, context_from_claims
from .clients import (
    InMemoryCatalogClient,
    InMemoryIngestionClient,
    InMemoryRetrievalClient,
)
from .http_clients import (
    HttpCatalogClient,
    HttpIngestionClient,
    HttpRetrievalClient,
)
from .context import clear_context, set_context, RequestContext as MCPContext
from . import tools as tool_impls
from .models import (
    DeleteDocumentArgs,
    GetDocumentArgs,
    GetReviewItemArgs,
    IngestArgs,
    JobStatusArgs,
    ListDocumentsArgs,
    ResolveReviewItemArgs,
    SearchArgs,
)

log = logging.getLogger(__name__)

# Headers that must never influence identity. Stripped defensively even
# though nothing in this service reads them.
_SPOOFABLE_IDENTITY_HEADERS = (
    "x-user-id",
    "x-group-id",
    "x-tenant-id",
    "x-roles",
    "x-forwarded-user",
)


@dataclass
class Settings:
    jwt_secret: str = ""
    jwt_public_key_pem: str = ""
    jwt_algorithms: tuple[str, ...] = ("HS256",)
    jwt_issuer: str = "druid-gateway"
    jwt_audience: str = "druid-mcp"
    allowed_origins: tuple[str, ...] = ()
    dev_fake_clients: bool = False
    # Downstream services the real HTTP clients call (spec §11). The fake
    # clients ignore these; the real ones fail closed when they are unset.
    retrieval_url: str = ""
    document_api_url: str = ""
    internal_secret: str = ""
    gateway_internal_auth: bool = False
    downstream_timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "Settings":
        origins = tuple(
            o.strip() for o in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
        )
        secret = os.getenv("JWT_SECRET", "")
        public_key = os.getenv("JWT_PUBLIC_KEY_PEM", "")
        algorithms: tuple[str, ...] = ("HS256",)
        key = secret
        if public_key:
            algorithms = ("RS256",)
            key = public_key
        try:
            timeout = float(os.getenv("MCP_DOWNSTREAM_TIMEOUT_SECONDS", "30") or 30)
        except ValueError:
            timeout = 30.0
        return cls(
            jwt_secret=key,
            jwt_public_key_pem=public_key,
            jwt_algorithms=algorithms,
            jwt_issuer=os.getenv("JWT_ISSUER", "druid-gateway"),
            jwt_audience=os.getenv("JWT_AUDIENCE", "druid-mcp"),
            allowed_origins=origins,
            dev_fake_clients=os.getenv("DRUID_MCP_DEV_FAKE_CLIENTS") == "1",
            retrieval_url=os.getenv("RETRIEVAL_URL", "").rstrip("/"),
            document_api_url=os.getenv("DOCUMENT_API_URL", "").rstrip("/"),
            internal_secret=os.getenv("INTERNAL_SECRET", ""),
            gateway_internal_auth=os.getenv("DRUID_AUTH_MODE") == "keycloak",
            downstream_timeout_seconds=timeout,
        )


class AuthMiddleware(BaseHTTPMiddleware):
    """Validate bearer tokens and install the trusted request context."""

    def __init__(self, app, verifier: TokenVerifier, allowed_origins: tuple[str, ...],
                 gateway_secret: str = ""):
        super().__init__(app)
        self._verifier = verifier
        self._allowed_origins = allowed_origins
        self._gateway_secret = gateway_secret

    def _gateway_context(self, request: Request) -> MCPContext:
        """Accept only a properly signed internal identity from the gateway."""
        get = request.headers.get
        user, group, roles, rid = (
            get("x-druid-user-id", ""), get("x-druid-group-id", ""),
            get("x-druid-roles", ""), get("x-druid-request-id", ""),
        )
        signature = get("x-druid-internal-sig", "")
        if not all((user, group, rid, signature)):
            raise AuthError("missing signed gateway identity")
        parsed_roles = tuple(sorted(set(x for x in roles.split(",") if x)))
        canonical = f"druid-v1|{user}|{group}|{','.join(parsed_roles)}|{rid}"
        expected = "v1:" + hmac.new(
            self._gateway_secret.encode(), canonical.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise AuthError("gateway signature mismatch")
        return MCPContext(user_id=user, group_id=group,
                          roles=frozenset(parsed_roles), request_id=rid)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        started = time.monotonic()
        # 1. Strip spoofable identity headers (defense in depth; unread anyway).
        for name in _SPOOFABLE_IDENTITY_HEADERS:
            if name in request.headers:
                # Starlette headers are immutable; note removal in the scope
                # so downstream code cannot see them.
                raw = [
                    (k, v)
                    for (k, v) in request.scope["headers"]
                    if k.decode("latin-1").lower() != name
                ]
                request.scope["headers"] = raw

        # 2. Origin validation (DNS-rebinding protection for browser clients).
        origin = request.headers.get("origin")
        if origin and origin not in self._allowed_origins:
            log.warning(
                "mcp auth → 403: origin %r not allowlisted path=%s",
                origin,
                request.url.path,
            )
            return JSONResponse({"error": "origin not allowed"}, status_code=403)

        # 3. Bearer token validation. Fail closed on anything unexpected.
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        try:
            if self._gateway_secret:
                ctx = self._gateway_context(request)
            else:
                claims = self._verifier.verify(token.strip())
                ctx = context_from_claims(claims)
        except AuthError as exc:
            log.warning("mcp auth → 401: %s path=%s", exc, request.url.path)
            return JSONResponse({"error": str(exc)}, status_code=401)

        # Install validated DB-derived context for this request only.
        token_handle = set_context(ctx)
        try:
            response = await call_next(request)
        finally:
            clear_context(token_handle)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        log.info(
            "mcp request: %s %s → %d in %.1fms group=%s request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            ctx.group_id,
            ctx.request_id,
        )
        return response


def _register_tools(mcp: FastMCP, deps: tool_impls.ServiceDeps) -> None:
    """Register the 8 tools, binding each to the shared service deps.

    The inner wrappers carry the implementation docstrings so the MCP tool
    descriptions include the data-not-instructions guidance.
    """

    def _doc(impl):
        def fix(wrapper):
            wrapper.__doc__ = impl.__doc__
            return wrapper

        return fix

    @mcp.tool()
    @_doc(tool_impls.search_knowledge_base)
    async def search_knowledge_base(
        query: str = Field(min_length=1, max_length=2000, description="Natural-language question or keyword query."),
        top_k: int = Field(default=8, ge=1, le=20, description="Max passages to return (1-20)."),
    ) -> dict:
        return await tool_impls.search_knowledge_base(deps, SearchArgs(query=query, top_k=top_k))

    @mcp.tool()
    @_doc(tool_impls.ingest_document)
    async def ingest_document(
        filename: str = Field(description="Display name of the file (bare name, never a path)."),
        mime_type: str = Field(description="One of: application/pdf, image/png, image/jpeg, image/tiff."),
        file_size_bytes: int = Field(ge=1, le=200 * 1024 * 1024),
        content_sha256: str = Field(description="Hex SHA-256 of the file bytes."),
    ) -> dict:
        return await tool_impls.ingest_document(
            deps,
            IngestArgs(filename=filename, mime_type=mime_type, file_size_bytes=file_size_bytes, content_sha256=content_sha256),
        )

    @mcp.tool()
    @_doc(tool_impls.get_ingestion_status)
    async def get_ingestion_status(job_id: str = Field(description="Opaque job ID from ingest_document.")) -> dict:
        return await tool_impls.get_ingestion_status(deps, JobStatusArgs(job_id=job_id))

    @mcp.tool()
    @_doc(tool_impls.list_documents)
    async def list_documents(
        review_required: bool | None = Field(default=None, description="True: only documents awaiting review."),
        status: str | None = Field(default=None, description="Filter by ingestion state enum."),
        limit: int = Field(default=50, ge=1, le=100),
        offset: int = Field(default=0, ge=0),
    ) -> dict:
        return await tool_impls.list_documents(
            deps, ListDocumentsArgs(review_required=review_required, status=status, limit=limit, offset=offset)
        )

    @mcp.tool()
    @_doc(tool_impls.get_document)
    async def get_document(
        document_id: str = Field(description="Opaque document ID."),
        include_artifacts: bool = Field(default=False, description="Include opaque artifact references."),
    ) -> dict:
        return await tool_impls.get_document(
            deps, GetDocumentArgs(document_id=document_id, include_artifacts=include_artifacts)
        )

    @mcp.tool()
    @_doc(tool_impls.get_review_item)
    async def get_review_item(review_item_id: str = Field(description="Opaque review item ID.")) -> dict:
        return await tool_impls.get_review_item(deps, GetReviewItemArgs(review_item_id=review_item_id))

    @mcp.tool()
    @_doc(tool_impls.resolve_review_item)
    async def resolve_review_item(
        review_item_id: str = Field(description="Opaque review item ID."),
        decision: str = Field(description="One of: accept, reroute, reject."),
        note: str | None = Field(default=None, max_length=500),
        confirm: bool = Field(default=False, description="Must be true to apply the decision."),
    ) -> dict:
        return await tool_impls.resolve_review_item(
            deps,
            ResolveReviewItemArgs(review_item_id=review_item_id, decision=decision, note=note, confirm=confirm),
        )

    @mcp.tool()
    @_doc(tool_impls.delete_document)
    async def delete_document(
        document_id: str = Field(description="Opaque document ID."),
        confirm: bool = Field(default=False, description="Must be true to enqueue deletion."),
    ) -> dict:
        return await tool_impls.delete_document(
            deps, DeleteDocumentArgs(document_id=document_id, confirm=confirm)
        )


def _default_deps(settings: Settings) -> tool_impls.ServiceDeps:
    if settings.dev_fake_clients:
        log.warning(
            "mcp_server using in-memory fake service clients "
            "(DRUID_MCP_DEV_FAKE_CLIENTS=1): tests/dev only, never real data"
        )
        return tool_impls.ServiceDeps(
            retrieval=InMemoryRetrievalClient(),
            ingestion=InMemoryIngestionClient(),
            catalog=InMemoryCatalogClient(),
        )
    missing = [
        name
        for name, value in (
            ("RETRIEVAL_URL", settings.retrieval_url),
            ("DOCUMENT_API_URL", settings.document_api_url),
            ("INTERNAL_SECRET", settings.internal_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Real downstream clients requested but misconfigured; missing: "
            + ", ".join(missing)
            + ". Set DRUID_MCP_DEV_FAKE_CLIENTS=1 for local development only."
        )
    timeout = settings.downstream_timeout_seconds
    return tool_impls.ServiceDeps(
        retrieval=HttpRetrievalClient(
            base_url=settings.retrieval_url,
            internal_secret=settings.internal_secret,
            timeout=timeout,
        ),
        ingestion=HttpIngestionClient(
            base_url=settings.document_api_url,
            internal_secret=settings.internal_secret,
            timeout=timeout,
        ),
        catalog=HttpCatalogClient(
            base_url=settings.document_api_url,
            internal_secret=settings.internal_secret,
            timeout=timeout,
        ),
    )


def create_app(
    settings: Settings | None = None,
    deps: tool_impls.ServiceDeps | None = None,
) -> Starlette:
    """Build the Streamable HTTP MCP application."""
    settings = settings or Settings.from_env()
    if settings.gateway_internal_auth:
        if not settings.internal_secret or settings.internal_secret == "change-me":
            raise RuntimeError("MCP gateway mode requires a strong INTERNAL_SECRET")
    elif not settings.jwt_secret:
        raise RuntimeError("JWT_SECRET or JWT_PUBLIC_KEY_PEM is required")
    deps = deps or _default_deps(settings)
    log.info(
        "starting mcp_server (streamable http): jwt_algs=%s jwt_issuer=%s "
        "jwt_audience=%s allowed_origins=%d dev_fake_clients=%s "
        "retrieval_url=%s document_api_url=%s downstream_timeout=%ss",
        ",".join(settings.jwt_algorithms),
        settings.jwt_issuer,
        settings.jwt_audience,
        len(settings.allowed_origins),
        settings.dev_fake_clients,
        settings.retrieval_url or "-",
        settings.document_api_url or "-",
        settings.downstream_timeout_seconds,
    )

    mcp = FastMCP("new-druid")
    _register_tools(mcp, deps)

    verifier = JWTVerifier(
        key=settings.jwt_secret,
        algorithms=settings.jwt_algorithms,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )
    inner = mcp.streamable_http_app()
    app = Starlette(routes=[Mount("/mcp", app=inner)])
    app.add_middleware(
        AuthMiddleware, verifier=verifier, allowed_origins=settings.allowed_origins,
        gateway_secret=settings.internal_secret if settings.gateway_internal_auth else "",
    )
    return app


def build_fastmcp_for_tests(deps: tool_impls.ServiceDeps) -> FastMCP:
    """Test helper: the bare FastMCP server without HTTP middleware."""
    mcp = FastMCP("new-druid-test")
    _register_tools(mcp, deps)
    return mcp


# Convenience for `uvicorn --factory mcp_server.server:create_app`.
__all__ = ["create_app", "build_fastmcp_for_tests", "Settings", "AuthMiddleware"]
