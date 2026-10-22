"""Upload validation: MIME sniffing, size caps, PDF page counts (spec §5).

The declared ``Content-Type`` of a multipart upload is untrusted. The file's
magic bytes are the authority; the declared type must either agree or be the
generic ``application/octet-stream``. Page counts come from the PDF trailer
via ``pypdf`` — the document is never fully parsed here.
"""

from __future__ import annotations

import io

#: MIME types the ingestion pipeline supports (§5 initial scope).
ALLOWED_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/tiff",
    }
)

_GENERIC_OCTET_STREAM = "application/octet-stream"


class UploadValidationError(ValueError):
    """Raised when an upload fails validation (→ HTTP 400)."""


class UploadTooLargeError(UploadValidationError):
    """Raised when an upload exceeds the byte cap (→ HTTP 413)."""


def sniff_mime(data: bytes) -> str | None:
    """Detect the MIME type from magic bytes. None when unrecognized."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if (
        data.startswith(b"RIFF")
        and len(data) >= 12
        and data[8:12] == b"WEBP"
    ):
        return "image/webp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    return None


def validate_mime(data: bytes, declared: str | None) -> str:
    """Return the trusted MIME type or raise :class:`UploadValidationError`.

    The sniffed type must be in the allowlist. The declared type must agree
    with it, unless the client sent the generic octet-stream.
    """
    sniffed = sniff_mime(data)
    if sniffed is None:
        raise UploadValidationError(
            "unrecognized file type: magic bytes match no supported format "
            "(PDF, PNG, JPEG, WebP, TIFF)"
        )
    if sniffed not in ALLOWED_MIME_TYPES:  # pragma: no cover - defensive
        raise UploadValidationError(f"unsupported file type: {sniffed}")
    declared = (declared or "").split(";")[0].strip().lower()
    if declared and declared != _GENERIC_OCTET_STREAM and declared != sniffed:
        raise UploadValidationError(
            f"declared Content-Type {declared!r} does not match "
            f"detected file type {sniffed!r}"
        )
    return sniffed


def count_pages(data: bytes, mime: str) -> int:
    """Return the page count: PDF trailer for PDFs, 1 for images."""
    if mime != "application/pdf":
        return 1
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf is required for PDF page counting") from exc
    try:
        stream = io.BytesIO(data) if isinstance(data, bytes) else data
        stream.seek(0)
        reader = PdfReader(stream)
        return len(reader.pages)
    except Exception as exc:
        raise UploadValidationError(
            f"could not read PDF structure: {exc}"
        ) from exc


__all__ = [
    "ALLOWED_MIME_TYPES",
    "UploadTooLargeError",
    "UploadValidationError",
    "count_pages",
    "sniff_mime",
    "validate_mime",
]
