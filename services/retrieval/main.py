"""Retrieval service HTTP entrypoint (port 8004).

Accepts the gateway's HMAC-signed internal identity headers and builds the
trusted :class:`RequestContext` from them — never from the request body.
The header contract mirrors ``services.gateway.druid_internal``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from services.common.request_context import RequestContext

from .citations import wrap_passage
from .config import RetrievalConfig
from .service import RetrievalService

log = logging.getLogger("retrieval")

_SIGNATURE_TTL_SECONDS = 300


def _verify_internal_headers(
    user: str | None,
    group: str | None,
    roles: str | None,
    request_id: str | None,
    timestamp: str | None,
    signature: str | None,
    secret: str,
) -> RequestContext:
    """Verify the gateway's HMAC-signed identity headers (fail closed)."""
    if not all([user, group, timestamp, signature]):
        raise HTTPException(status_code=401, detail="missing internal identity")
    try:
        ts = float(timestamp or "")
    except ValueError:
        raise HTTPException(status_code=401, detail="bad identity timestamp")
    if abs(time.time() - ts) > _SIGNATURE_TTL_SECONDS:
        raise HTTPException(status_code=401, detail="stale identity")
    payload = f"{user}|{group}|{roles or ''}|{request_id or ''}|{timestamp}".encode()
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature or ""):
        raise HTTPException(status_code=401, detail="bad identity signature")
    return RequestContext(
        user_id=user or "",
        group_id=group or "",
        roles=tuple((roles or "").split(",")) if roles else (),
        request_id=request_id or "",
    )


app = FastAPI(title="new_DRUID retrieval service")

_service: RetrievalService | None = None


def get_service() -> RetrievalService:
    if _service is None:
        raise HTTPException(status_code=503, detail="retrieval service not wired")
    return _service


def wire(service: RetrievalService) -> None:
    """Inject the fully-constructed RetrievalService (done at startup)."""
    global _service
    _service = service


@app.post("/search")
async def search(
    request: Request,
    x_druid_user: str | None = Header(default=None),
    x_druid_group: str | None = Header(default=None),
    x_druid_roles: str | None = Header(default=None),
    x_druid_request_id: str | None = Header(default=None),
    x_druid_timestamp: str | None = Header(default=None),
    x_druid_signature: str | None = Header(default=None),
):
    secret = os.environ.get("INTERNAL_SECRET", "")
    if not secret:
        raise HTTPException(status_code=500, detail="INTERNAL_SECRET not configured")
    context = _verify_internal_headers(
        x_druid_user,
        x_druid_group,
        x_druid_roles,
        x_druid_request_id,
        x_druid_timestamp,
        x_druid_signature,
        secret,
    )
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    results = get_service().search(
        context,
        query,
        task_hint=body.get("task_hint"),
        expand_neighbors=body.get("expand_neighbors"),
    )
    return {
        "results": [r.to_dict() for r in results],
        "passages": [wrap_passage(r) for r in results],
    }


@app.exception_handler(Exception)
def _unhandled(request: Request, exc: Exception):  # noqa: ARG001
    log.exception("unhandled retrieval error")
    return JSONResponse(status_code=500, detail="internal retrieval error")
