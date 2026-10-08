"""document_api FastAPI application (spec §11 API surface).

Runs behind the gateway: the gateway authenticates the bearer token,
enforces quotas, strips spoofable headers, and forwards the request with
HMAC-signed ``X-Druid-*`` identity headers. This service verifies that
signature on every request and fails closed without it (§4).

Uvicorn entry point: ``main:app`` (see Dockerfile).
"""

from __future__ import annotations

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
from routes.documents import router as documents_router
from routes.review import router as review_router
from settings import DocumentApiSettings
from storage import ObjectStore


def _register_error_handlers(app: FastAPI) -> None:
    """Map domain errors to HTTP codes. Auth failures never leak details."""

    @app.exception_handler(InternalAuthError)
    @app.exception_handler(TenantContextError)
    async def _unauthorized(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})

    @app.exception_handler(AuthorizationError)
    async def _forbidden(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})

    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: Exception) -> JSONResponse:
        # Tenant-scoped lookup: "not found" also covers "not yours".
        return JSONResponse(status_code=404, content={"detail": "not found"})

    @app.exception_handler(ConflictError)
    async def _conflict(request: Request, exc: Exception) -> JSONResponse:
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
        app.state.session_factory = (
            session_factory or session_factory_from_settings(settings)
        )
        app.state.object_store = object_store or default_object_store(settings)
        yield

    app = FastAPI(title="new_DRUID document API", version="2.1.0", lifespan=lifespan)
    _register_error_handlers(app)
    # The injected settings win over the process-global cached ones so tests
    # (and any embedder) control configuration deterministically.
    app.dependency_overrides[get_settings] = lambda: settings
    app.include_router(documents_router)
    app.include_router(review_router)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


# Uvicorn entry point (Dockerfile CMD: uvicorn main:app).
app = create_app()


__all__ = ["app", "create_app"]
