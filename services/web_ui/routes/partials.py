"""htmx partial endpoints (spec §16): HTML fragments, not pages.

* ``/partials/documents/rows`` — refreshed table body (polling).
* ``/partials/search`` — search results fragment.
* ``/upload/session*`` — chunked, resumable upload protocol (see
  ``upload_sessions.py``). The browser splits the file; ``complete``
  assembles it and forwards ONE multipart upload to the gateway, which
  enforces the authoritative size/quota limits.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from api_client import ApiError
from deps import NotAuthenticated

from . import (
    api_for,
    current_identity,
    enrich_documents,
    get_templates,
    login_redirect,
    page_context,
)

log = logging.getLogger(__name__)
router = APIRouter()

# Conservative client-side pre-validation. The gateway/document_api re-validates
# authoritatively (magic bytes, page limits); this only fails fast on obvious
# mistakes. Extensions mirror the backend allowlist: PDF + page images.
_ALLOWED_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif")
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._\- ]")


def _safe_filename(name: str | None) -> str:
    name = (name or "upload").strip().replace("\\", "/").split("/")[-1]
    name = _FILENAME_SAFE.sub("_", name).strip(" .")
    return name[:200] or "upload"


def _validate_upload(filename: str, size_bytes: int, max_mb: int) -> str | None:
    """Return an error message, or None when the upload looks acceptable."""
    if size_bytes <= 0:
        return "file is empty"
    if size_bytes > max_mb * 1024 * 1024:
        return f"file exceeds the {max_mb} MB limit"
    lowered = filename.lower()
    if not lowered.endswith(_ALLOWED_EXTENSIONS):
        return (
            "unsupported file type (PDF, PNG, JPEG, WebP, TIFF). "
            "The backend re-validates by file contents."
        )
    return None


def _require_identity_fragment(request: Request):
    """Identity for fragment endpoints: 401 JSON on failure (no redirect)."""
    try:
        return current_identity(request)
    except NotAuthenticated as exc:
        return JSONResponse(status_code=401, content={"detail": str(exc)})


@router.get("/partials/documents/rows", response_class=HTMLResponse)
async def document_rows(request: Request):
    identity = _require_identity_fragment(request)
    if isinstance(identity, JSONResponse):
        return identity
    try:
        client = api_for(identity, request)
        data = await client.list_documents(limit=100)
        documents = await enrich_documents(client, data.get("documents", []))
    except ApiError as exc:
        if exc.status_code == 401:
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
        return HTMLResponse(
            '<tr><td colspan="6" class="error">could not refresh document list</td></tr>',
            status_code=502,
        )
    return get_templates().TemplateResponse(
        "_doc_rows.html",
        page_context(request, identity, documents=documents),
    )


@router.post("/partials/search", response_class=HTMLResponse)
async def search_results(request: Request, q: str = Form(...), top_k: int = Form(8)):
    identity = _require_identity_fragment(request)
    if isinstance(identity, JSONResponse):
        return identity
    query = (q or "").strip()[:2000]
    if not query:
        return HTMLResponse('<p class="muted">Enter a question to search.</p>')
    top_k = max(1, min(20, top_k))
    try:
        passages = await api_for(identity, request).search(query, top_k=top_k)
    except ApiError as exc:
        if exc.status_code == 401:
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
        return HTMLResponse(
            '<p class="error">Search failed. Please try again.</p>', status_code=502
        )
    return get_templates().TemplateResponse(
        "_search_results.html",
        page_context(request, identity, passages=passages, query=query),
    )


# ---------------------------------------------------------------------------
# Chunked upload protocol
# ---------------------------------------------------------------------------


@router.post("/upload/session")
async def upload_session_create(
    request: Request,
    filename: str = Form(...),
    content_type: str = Form(default="application/octet-stream"),
    total_chunks: int = Form(...),
    total_bytes: int = Form(...),
):
    identity = _require_identity_fragment(request)
    if isinstance(identity, JSONResponse):
        return identity
    settings = request.app.state.web_ui_settings
    filename = _safe_filename(filename)
    if total_chunks < 1 or total_chunks > 4096:
        return JSONResponse(
            status_code=400, content={"detail": "invalid total_chunks"}
        )
    err = _validate_upload(filename, total_bytes, settings.max_upload_mb)
    if err:
        return JSONResponse(status_code=400, content={"detail": err})
    store = request.app.state.upload_sessions
    session = store.create(filename, content_type, total_chunks)
    log.info(
        "upload session %s started: file=%r chunks=%d group=%s",
        session.session_id,
        filename,
        total_chunks,
        identity.group_id,
    )
    return {"session_id": session.session_id}


@router.get("/upload/session/{session_id}")
async def upload_session_state(request: Request, session_id: str):
    identity = _require_identity_fragment(request)
    if isinstance(identity, JSONResponse):
        return identity
    session = request.app.state.upload_sessions.get(session_id)
    if session is None:
        return JSONResponse(
            status_code=404, content={"detail": "unknown or expired session"}
        )
    return {"received": sorted(session.chunks.keys()), "total": session.total_chunks}


@router.post("/upload/session/{session_id}/chunk")
async def upload_session_chunk(
    request: Request,
    session_id: str,
    index: int = Query(...),
    chunk: UploadFile = File(...),
):
    identity = _require_identity_fragment(request)
    if isinstance(identity, JSONResponse):
        return identity
    settings = request.app.state.web_ui_settings
    data = await chunk.read()
    if len(data) > settings.upload_chunk_mb * 1024 * 1024 + 1024 * 1024:
        return JSONResponse(status_code=400, content={"detail": "chunk too large"})
    store = request.app.state.upload_sessions
    try:
        session = store.add_chunk(session_id, index, data)
    except KeyError:
        return JSONResponse(
            status_code=404, content={"detail": "unknown or expired session"}
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return {"received": sorted(session.chunks.keys()), "total": session.total_chunks}


@router.post("/upload/session/{session_id}/complete")
async def upload_session_complete(request: Request, session_id: str):
    identity = _require_identity_fragment(request)
    if isinstance(identity, JSONResponse):
        return identity
    settings = request.app.state.web_ui_settings
    store = request.app.state.upload_sessions
    session = store.get(session_id)
    if session is None:
        return JSONResponse(
            status_code=404, content={"detail": "unknown or expired session"}
        )
    if not session.is_complete():
        return JSONResponse(
            status_code=400, content={"detail": "not all chunks received"}
        )
    data = session.assemble()
    store.pop(session_id)
    err = _validate_upload(session.filename, len(data), settings.max_upload_mb)
    if err:
        return JSONResponse(status_code=400, content={"detail": err})
    log.info(
        "upload session %s complete: file=%r bytes=%d group=%s",
        session_id,
        session.filename,
        len(data),
        identity.group_id,
    )
    try:
        result = await api_for(identity, request).upload_document(
            session.filename, data, session.content_type
        )
    except ApiError as exc:
        status = 401 if exc.status_code == 401 else 502
        return JSONResponse(
            status_code=status,
            content={"detail": exc.detail or "upload failed"},
        )
    return JSONResponse(status_code=201, content=result)


__all__ = ["router"]
