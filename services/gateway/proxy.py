"""Upstream forwarding for the gateway.

After authentication, quota checks, and spoofable-header stripping, the
gateway forwards the request to the appropriate internal service over the
authenticated internal connection: the original bearer token plus the
HMAC-signed ``X-Druid-*`` identity headers (see
:mod:`services.gateway.druid_internal`).
"""

from __future__ import annotations

import logging
import time

import httpx
from fastapi import Request
from fastapi.responses import Response

from services.common.request_context import RequestContext

from .druid_internal import SPOOFABLE_HEADERS, build_internal_headers

log = logging.getLogger(__name__)

#: Hop-by-hop headers that must never be forwarded.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",  # httpx recomputes this from the body
        "host",  # httpx sets this from the target URL
    }
)

_ORIGINAL_ASYNC_CLIENT = httpx.AsyncClient
_SHARED_CLIENT: httpx.AsyncClient | None = None


def _get_shared_client() -> httpx.AsyncClient:
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None or _SHARED_CLIENT.is_closed:
        _SHARED_CLIENT = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
            )
        )
    return _SHARED_CLIENT


async def forward_request(
    request: Request,
    context: RequestContext,
    upstream_base: str,
    *,
    internal_secret: str,
    timeout_seconds: float = 30.0,
) -> Response:
    """Forward an authenticated request to an internal service.

    The trusted context travels as HMAC-signed ``X-Druid-*`` headers; the
    original ``Authorization`` bearer token is also forwarded so the backend
    may validate the original token instead (§4).
    """
    target = upstream_base.rstrip("/") + request.url.path
    if request.url.query:
        target += "?" + request.url.query

    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        lowered = name.lower()
        if lowered in _HOP_BY_HOP or lowered in SPOOFABLE_HEADERS or lowered == "x-group-selection":
            continue
        headers[name] = value
    headers.update(build_internal_headers(context, internal_secret))

    body = request.stream() if request.headers.get("content-type", "").startswith("multipart/") else await request.body()
    started = time.monotonic()
    log.info(
        "forward start: %s %s → %s request_id=%s group=%s",
        request.method,
        request.url.path,
        upstream_base,
        context.request_id,
        context.group_id,
    )
    try:
        if httpx.AsyncClient is not _ORIGINAL_ASYNC_CLIENT:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                upstream = await client.request(
                    request.method, target, headers=headers, content=body
                )
        else:
            client = _get_shared_client()
            try:
                upstream = await client.request(
                    request.method,
                    target,
                    headers=headers,
                    content=body,
                    timeout=timeout_seconds,
                )
            except RuntimeError:
                # A streamed request cannot be safely replayed after partial forwarding.
                global _SHARED_CLIENT
                _SHARED_CLIENT = None
                raise
    except httpx.TimeoutException:
        log.warning(
            "forward timeout → 504: %s %s request_id=%s",
            request.method,
            request.url.path,
            context.request_id,
        )
        return Response(
            content='{"detail":"upstream service timed out"}',
            status_code=504,
            media_type="application/json",
        )
    except httpx.HTTPError as exc:
        log.warning(
            "forward failed → 502: %s %s request_id=%s error=%s",
            request.method,
            request.url.path,
            context.request_id,
            exc,
        )
        return Response(
            content='{"detail":"upstream service unavailable"}',
            status_code=502,
            media_type="application/json",
        )
    elapsed_ms = (time.monotonic() - started) * 1000.0
    log.info(
        "forward finish: %s %s → %d in %.1fms request_id=%s",
        request.method,
        request.url.path,
        upstream.status_code,
        elapsed_ms,
        context.request_id,
    )

    passthrough = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower()
        not in ("content-encoding", "transfer-encoding", "connection", "content-length")
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=passthrough,
        media_type=upstream.headers.get(
            "content-type", "application/octet-stream"
        ),
    )
