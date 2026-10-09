"""document_api FastAPI application (spec §11 API surface).

Runs behind the gateway: the gateway authenticates the bearer token,
enforces quotas, strips spoofable headers, and forwards the request with
HMAC-signed ``X-Druid-*`` identity headers. This service verifies that
signature on every request and fails closed without it (§4).

Uvicorn entry point: ``main:app`` (see Dockerfile).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from services.catalog.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    TenantContextError,
)

from deps import (
    default_object_store,
    get_settings,
    session_factory_from_settings,
)
from internal_auth import InternalAuthError
from routes.admin import router as admin_router
from routes.documents import router as documents_router
from routes.review import router as review_router
from settings import DocumentApiSettings
from storage import ObjectStore

log = logging.getLogger(__name__)


def _register_error_handlers(app: FastAPI) -> None:
    """Map domain errors to HTTP codes. Auth failures never leak details."""

    def _req_id(request: Request) -> str:
        return request.headers.get("x-druid-request-id", "") or "-"

    @app.exception_handler(InternalAuthError)
    @app.exception_handler(TenantContextError)
    async def _unauthorized(request: Request, exc: Exception) -> JSONResponse:
        log.warning(
            "request → 401: %s path=%s request_id=%s",
            type(exc).__name__,
            request.url.path,
            _req_id(request),
        )
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})

    @app.exception_handler(AuthorizationError)
    async def _forbidden(request: Request, exc: Exception) -> JSONResponse:
        log.warning(
            "request → 403: path=%s request_id=%s",
            request.url.path,
            _req_id(request),
        )
        return JSONResponse(status_code=403, content={"detail": "forbidden"})

    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: Exception) -> JSONResponse:
        # Tenant-scoped lookup: "not found" also covers "not yours".
        log.info(
            "request → 404: path=%s request_id=%s",
            request.url.path,
            _req_id(request),
        )
        return JSONResponse(status_code=404, content={"detail": "not found"})

    @app.exception_handler(ConflictError)
    async def _conflict(request: Request, exc: Exception) -> JSONResponse:
        log.warning(
            "request → 409: %s path=%s request_id=%s",
            exc,
            request.url.path,
            _req_id(request),
        )
        return JSONResponse(status_code=409, content={"detail": str(exc)})


def create_app(
    *,
    settings: DocumentApiSettings | None = None,
    session_factory: Callable[[], Session] | None = None,
    object_store: ObjectStore | None = None,
) -> FastAPI:
    """Build the FastAPI app with injectable infrastructure (tests override)."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info(
            "starting document_api: ingest_queue=%s delete_queue=%s "
            "object_store=%s rustfs_bucket=%s max_upload_mb=%d "
            "max_pages_per_document=%d redis_configured=%s database_configured=%s",
            settings.ingest_queue,
            settings.delete_queue,
            settings.object_store,
            settings.rustfs_bucket,
            settings.max_upload_mb,
            settings.max_pages_per_document,
            bool(settings.redis_url),
            bool(settings.database_url),
        )
        app.state.session_factory = (
            session_factory or session_factory_from_settings(settings)
        )
        app.state.object_store = object_store or default_object_store(settings)
        log.info("document_api ready")
        yield

    app = FastAPI(title="new_DRUID document API", version="2.1.0", lifespan=lifespan)
    _register_error_handlers(app)
    # The injected settings win over the process-global cached ones so tests
    # (and any embedder) control configuration deterministically.
    app.dependency_overrides[get_settings] = lambda: settings
    app.include_router(admin_router)
    app.include_router(documents_router)
    app.include_router(review_router)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


# Uvicorn entry point (Dockerfile CMD: uvicorn main:app).
app = create_app()


__all__ = ["app", "create_app"]
