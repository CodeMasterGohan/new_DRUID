"""web_ui FastAPI application (spec §16: Web UI — Tenant Document Management).

Thin human layer over the gateway/document_api: validates the session JWT
with the gateway's contract, then forwards API calls to the gateway with
the user's own token. No direct database/Milvus/RustFS access.

Uvicorn entry point: ``main:app`` (see Dockerfile).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auth import JWTSettings
from deps import NotAuthenticated
from settings import WebUISettings
from upload_sessions import UploadSessionStore

import routes as route_helpers
from routes import auth_pages, pages, partials, oidc_pages, group_pages

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

HERE = Path(__file__).resolve().parent


def _jwt_settings(ui: WebUISettings) -> JWTSettings:
    algorithms = tuple(
        a.strip().upper() for a in ui.jwt_algorithms.split(",") if a.strip()
    )
    return JWTSettings(
        secret=ui.jwt_secret,
        algorithms=algorithms or ("HS256",),
        issuer=ui.jwt_issuer,
        audience=ui.jwt_audience,
        jwks_url=ui.jwks_url,
    )


def create_app(
    ui_settings: WebUISettings | None = None,
    api_transport=None,
) -> FastAPI:
    ui = ui_settings or WebUISettings()
    app = FastAPI(title="DRUID Web UI", docs_url=None, redoc_url=None)
    from services.common.upload_limits import UploadBodyLimit
    app.add_middleware(UploadBodyLimit, max_bytes=(ui.max_upload_mb + 1) * 1024 * 1024,
                       concurrency=int(os.environ.get('UPLOAD_CONCURRENCY', '4')))


    app.state.web_ui_settings = ui
    app.state.jwt_settings = _jwt_settings(ui)
    app.state.upload_sessions = UploadSessionStore(
        ttl_seconds=ui.upload_session_ttl_seconds
    )
    # Test hook: an httpx.AsyncBaseTransport that stubs the gateway.
    # None in production.
    app.state.api_transport = api_transport

    route_helpers.templates = Jinja2Templates(
        directory=str(HERE / "templates")
    )
    app.mount(
        "/static", StaticFiles(directory=str(HERE / "static")), name="static"
    )

    # Strip spoofable identity headers at the edge, mirroring the gateway:
    # identity comes from the validated session token ONLY.
    @app.middleware("http")
    async def _strip_spoofable_headers(request: Request, call_next):
        for header in (
            "x-group-id",
            "x-user-id",
            "x-druid-user-id",
            "x-druid-group-id",
            "x-druid-roles",
            "x-druid-internal-sig",
            "x-druid-request-id",
        ):
            if header in request.headers:
                # Starlette headers are immutable; rebuild the scope.
                raw = [
                    (k, v)
                    for (k, v) in request.scope["headers"]
                    if k.decode("latin-1").lower() != header
                ]
                request.scope["headers"] = raw
        return await call_next(request)

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, exc: NotAuthenticated):
        # Page routes redirect; fragment/API routes get JSON 401.
        if request.url.path.startswith(("/partials/", "/upload/session")):
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
        return RedirectResponse(url="/login", status_code=303)

    app.include_router(auth_pages.router)
    app.include_router(oidc_pages.router)
    app.include_router(group_pages.router)
    app.include_router(pages.router)
    app.include_router(partials.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    log.info(
        "web_ui starting: gateway_url=%s cookie_secure=%s",
        ui.gateway_url,
        ui.cookie_secure,
    )
    return app


app = create_app()


__all__ = ["app", "create_app"]
