"""HTML pages (spec §16): Upload, Documents, Review queue, Quarantine,
Search, and the admin views. Every page requires a validated session;
tenant identity comes from the token only.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from api_client import AdminNotAvailable, ApiError
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


def _handle_api_error(request: Request, exc: ApiError):
    """Map downstream failures to UI responses without leaking details."""
    if exc.status_code == 401:
        return login_redirect(request)
    tmpl = get_templates().TemplateResponse(
        "error.html",
        {
            "request": request,
            "status": exc.status_code,
            "detail": "the backend request failed",
        },
        status_code=exc.status_code if exc.status_code < 600 else 502,
    )
    return tmpl


@router.get("/", include_in_schema=False)
def index(request: Request):
    try:
        current_identity(request)
    except NotAuthenticated:
        return login_redirect(request)
    return RedirectResponse(url="/documents", status_code=303)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    try:
        identity = current_identity(request)
    except NotAuthenticated:
        return login_redirect(request)
    settings = request.app.state.web_ui_settings
    return get_templates().TemplateResponse(
        "upload.html",
        page_context(
            request,
            identity,
            max_upload_mb=settings.max_upload_mb,
            chunk_mb=settings.upload_chunk_mb,
            quota_pages_per_day=1000,  # §4 default, informational
        ),
    )


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


@router.get("/documents", response_class=HTMLResponse)
async def documents_page(request: Request):
    try:
        identity = current_identity(request)
    except NotAuthenticated:
        return login_redirect(request)
    try:
        client = api_for(identity, request)
        data = await client.list_documents(limit=100)
        documents = await enrich_documents(client, data.get("documents", []))
    except ApiError as exc:
        return _handle_api_error(request, exc)
    return get_templates().TemplateResponse(
        "documents.html",
        page_context(request, identity, documents=documents),
    )


@router.get("/documents/{document_id}", response_class=HTMLResponse)
async def document_detail_page(request: Request, document_id: str):
    try:
        identity = current_identity(request)
    except NotAuthenticated:
        return login_redirect(request)
    client = api_for(identity, request)
    try:
        doc = await client.get_document(document_id)
        try:
            status = await client.get_document_status(document_id)
        except ApiError:
            status = None
    except ApiError as exc:
        return _handle_api_error(request, exc)
    return get_templates().TemplateResponse(
        "document_detail.html",
        page_context(request, identity, document=doc, job_status=status),
    )


@router.post("/documents/{document_id}/delete")
async def document_delete(request: Request, document_id: str):
    try:
        identity = current_identity(request)
    except NotAuthenticated:
        return login_redirect(request)
    try:
        await api_for(identity, request).delete_document(document_id)
    except ApiError as exc:
        return _handle_api_error(request, exc)
    return RedirectResponse(url="/documents", status_code=303)


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------


@router.get("/review", response_class=HTMLResponse)
async def review_page(request: Request, status: str = Query(default="pending")):
    try:
        identity = current_identity(request)
    except NotAuthenticated:
        return login_redirect(request)
    if status not in ("pending", "accepted", "re-routed", "rejected"):
        status = "pending"
    try:
        data = await api_for(identity, request).list_review_items(
            status=status, limit=100
        )
    except ApiError as exc:
        return _handle_api_error(request, exc)
    return get_templates().TemplateResponse(
        "review.html",
        page_context(request, identity, items=data.get("items", []), status=status),
    )


@router.get("/review/{item_id}", response_class=HTMLResponse)
async def review_detail_page(request: Request, item_id: str):
    try:
        identity = current_identity(request)
    except NotAuthenticated:
        return login_redirect(request)
    try:
        item = await api_for(identity, request).get_review_item(item_id)
    except ApiError as exc:
        return _handle_api_error(request, exc)
    return get_templates().TemplateResponse(
        "review_detail.html", page_context(request, identity, item=item)
    )


@router.post("/review/{item_id}/resolve")
async def review_resolve(
    request: Request,
    item_id: str,
    decision: str = Form(...),
    detail: str = Form(default=""),
):
    try:
        identity = current_identity(request)
    except NotAuthenticated:
        return login_redirect(request)
    # UI-level gate (the backend enforces again — defense in depth).
    if not identity.can_review:
        return get_templates().TemplateResponse(
            "error.html",
            {
                "request": request,
                "status": 403,
                "detail": "resolving review items requires the reviewer role",
            },
            status_code=403,
        )
    if decision not in ("accepted", "re-routed", "rejected"):
        return get_templates().TemplateResponse(
            "error.html",
            {"request": request, "status": 400, "detail": "invalid decision"},
            status_code=400,
        )
    try:
        await api_for(identity, request).resolve_review_item(
            item_id, decision, detail or None
        )
    except ApiError as exc:
        return _handle_api_error(request, exc)
    return RedirectResponse(url="/review", status_code=303)


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------


@router.get("/quarantine", response_class=HTMLResponse)
async def quarantine_page(request: Request):
    """FAILED documents with diagnostics. Retry re-uploads via /upload."""
    try:
        identity = current_identity(request)
    except NotAuthenticated:
        return login_redirect(request)
    try:
        data = await api_for(identity, request).list_documents(limit=100)
    except ApiError as exc:
        return _handle_api_error(request, exc)
    failed = [
        d
        for d in data.get("documents", [])
        if (d.get("active_version") or {}).get("status") == "FAILED"
        or d.get("status") == "FAILED"
    ]
    return get_templates().TemplateResponse(
        "quarantine.html", page_context(request, identity, documents=failed)
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request):
    try:
        identity = current_identity(request)
    except NotAuthenticated:
        return login_redirect(request)
    return get_templates().TemplateResponse(
        "search.html", page_context(request, identity, passages=None, query="")
    )


# ---------------------------------------------------------------------------
# Admin (admin role only)
# ---------------------------------------------------------------------------


def _require_admin(request: Request, identity) -> HTMLResponse | None:
    if not identity.is_admin:
        return get_templates().TemplateResponse(
            "error.html",
            {
                "request": request,
                "status": 403,
                "detail": "admin role required",
            },
            status_code=403,
        )
    return None


@router.get("/admin/documents", response_class=HTMLResponse)
async def admin_documents_page(request: Request, tenant: str = Query(default="")):
    try:
        identity = current_identity(request)
    except NotAuthenticated:
        return login_redirect(request)
    denied = _require_admin(request, identity)
    if denied is not None:
        return denied
    backend_missing = False
    documents: list = []
    try:
        data = await api_for(identity, request).admin_list_documents(
            tenant=tenant or None, limit=100
        )
        documents = data.get("documents", [])
    except AdminNotAvailable:
        backend_missing = True
    except ApiError as exc:
        return _handle_api_error(request, exc)
    return get_templates().TemplateResponse(
        "admin/documents.html",
        page_context(
            request,
            identity,
            documents=documents,
            tenant=tenant,
            backend_missing=backend_missing,
        ),
    )


@router.get("/admin/jobs", response_class=HTMLResponse)
async def admin_jobs_page(request: Request, tenant: str = Query(default="")):
    try:
        identity = current_identity(request)
    except NotAuthenticated:
        return login_redirect(request)
    denied = _require_admin(request, identity)
    if denied is not None:
        return denied
    backend_missing = False
    jobs: list = []
    try:
        data = await api_for(identity, request).admin_list_jobs(
            tenant=tenant or None, limit=100
        )
        jobs = data.get("jobs", [])
    except AdminNotAvailable:
        backend_missing = True
    except ApiError as exc:
        return _handle_api_error(request, exc)
    return get_templates().TemplateResponse(
        "admin/jobs.html",
        page_context(
            request, identity, jobs=jobs, tenant=tenant, backend_missing=backend_missing
        ),
    )


@router.get("/admin/quotas", response_class=HTMLResponse)
async def admin_quotas_page(request: Request):
    try:
        identity = current_identity(request)
    except NotAuthenticated:
        return login_redirect(request)
    denied = _require_admin(request, identity)
    if denied is not None:
        return denied
    backend_missing = False
    quotas: list = []
    try:
        data = await api_for(identity, request).admin_get_quotas()
        quotas = data.get("tenants", [])
    except AdminNotAvailable:
        backend_missing = True
    except ApiError as exc:
        return _handle_api_error(request, exc)
    return get_templates().TemplateResponse(
        "admin/quotas.html",
        page_context(
            request,
            identity,
            quotas=quotas,
            backend_missing=backend_missing,
            default_ingest_pages_per_day=1000,  # §4 defaults, informational
            default_query_qps=10,
        ),
    )


__all__ = ["router"]
