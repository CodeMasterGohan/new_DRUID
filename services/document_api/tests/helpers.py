"""Test helpers: gateway-signed headers and synthetic upload fixtures."""

from __future__ import annotations

import base64
import io
import uuid

from services.common.request_context import RequestContext

# Gateway's signer: tests prove byte-exact cross-compatibility between the
# gateway's build_internal_headers and document_api's verifier.
from services.gateway.druid_internal import build_internal_headers


def signed_headers(
    secret: str,
    *,
    user_id: str = "user-1",
    group_id: str = "tenant-a",
    roles: tuple[str, ...] = (),
    request_id: str | None = None,
) -> dict[str, str]:
    """Build gateway-signed X-Druid-* headers using the gateway's own signer."""
    ctx = RequestContext(
        user_id=user_id,
        group_id=group_id,
        roles=roles,
        request_id=request_id or f"req-{uuid.uuid4().hex[:12]}",
    )
    return build_internal_headers(ctx, secret)


def make_pdf(num_pages: int = 1) -> bytes:
    """Build a minimal multi-page PDF with pypdf (no external files)."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_png() -> bytes:
    """Minimal 1x1 PNG."""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
