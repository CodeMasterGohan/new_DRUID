"""Upstream forwarding for the gateway.

After authentication, quota checks, and spoofable-header stripping, the
gateway forwards the request to the appropriate internal service over the
authenticated internal connection: the original bearer token plus the
HMAC-signed ``X-Druid-*`` identity headers (see
:mod:`services.gateway.druid_internal`).
"""

from __future__ import annotations

import httpx
from fastapi import Request
from fastapi.responses import Response

from services.common.request_context import RequestContext

from .druid_internal import SPOOFABLE_HEADERS, build_internal_headers

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
        if lowered in _HOP_BY_HOP or lowered in SPOOFABLE_HEADERS:
            continue
        headers[name] = value
    headers.update(build_internal_headers(context, internal_secret))

    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            upstream = await client.request(
                request.method, target, headers=headers, content=body
            )
    except httpx.TimeoutException:
        return Response(
            content='{"detail":"upstream service timed out"}',
            status_code=504,
            media_type="application/json",
        )
    except httpx.HTTPError:
        return Response(
            content='{"detail":"upstream service unavailable"}',
            status_code=502,
            media_type="application/json",
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
