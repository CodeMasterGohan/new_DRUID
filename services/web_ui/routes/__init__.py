"""Shared helpers for web_ui routes."""

from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from api_client import DruidApiClient
from deps import Identity, NotAuthenticated, get_identity

class CompatibleTemplates(Jinja2Templates):
    """Keep the legacy name/context call shape while using Starlette's request-first API."""

    def TemplateResponse(self, *args, **kwargs):
        if args and isinstance(args[0], str):
            name = args[0]
            context = args[1] if len(args) > 1 else kwargs.pop("context", {})
            request = context.get("request")
            if request is None:
                raise ValueError("template context must include the request")
            return super().TemplateResponse(request, name, context, **kwargs)
        return super().TemplateResponse(*args, **kwargs)


templates: Jinja2Templates | None = None

_ENRICH_CONCURRENCY = 8


def get_templates() -> Jinja2Templates:
    assert templates is not None, "templates not configured"
    return templates


def current_identity(request: Request) -> Identity:
    """Trusted identity from the session cookie. Raises NotAuthenticated."""
    return get_identity(request, request.app.state.jwt_settings)


def api_for(identity: Identity, request: Request) -> DruidApiClient:
    """Backend client bound to the request's own token.

    The downstream gateway derives tenant identity from this token alone.
    """
    settings = request.app.state.web_ui_settings
    transport = request.app.state.api_transport  # None in prod, mock in tests
    return DruidApiClient(
        gateway_url=settings.gateway_url,
        token=identity.token,
        selected_group=identity.group_id,
        timeout_seconds=settings.forward_timeout_seconds,
        transport=transport,
    )


def login_redirect(request: Request) -> RedirectResponse:
    nxt = request.url.path
    target = "/login" if nxt in ("", "/") else f"/login?next={nxt}"
    return RedirectResponse(url=target, status_code=303)


#: Post-action confirmations, keyed by the ``?notice=`` query param. Only
#: these fixed strings are ever rendered — the param value itself is not.
NOTICES = {
    "deleted": "Deletion queued. The document disappears once every store confirms.",
    "resolved": "Review item resolved.",
}

#: Top-level nav sections, matched by path prefix (longest first).
_NAV_SECTIONS = (
    ("/admin/documents", "admin-documents"),
    ("/admin/groups", "admin-groups"),
    ("/groups", "groups"),
    ("/admin/jobs", "admin-jobs"),
    ("/admin/quotas", "admin-quotas"),
    ("/upload", "upload"),
    ("/documents", "documents"),
    ("/review", "review"),
    ("/quarantine", "quarantine"),
    ("/search", "search"),
)


def _nav_section(path: str) -> str | None:
    for prefix, name in _NAV_SECTIONS:
        if path == prefix or path.startswith(prefix + "/"):
            return name
    return None


def page_context(request: Request, identity: Identity, **extra):
    return {
        "request": request,
        "identity": identity,
        "is_admin": identity.is_admin or getattr(request.state, "system_admin", False),
        "can_review": identity.can_review,
        "nav_active": _nav_section(request.url.path),
        "notice": NOTICES.get(request.query_params.get("notice", "")),
        **extra,
    }


async def enrich_documents(client, documents: list[dict]) -> list[dict]:
    """Attach active-version status/page count to each document summary.

    The list endpoint returns summaries only; version state lives on the
    detail endpoint. Enriches with bounded concurrency so a 100-row table
    does not serialize 100 round-trips.
    ``latest_status`` is the newest version's status (the detail endpoint
    orders versions newest first), used to surface failed re-ingests.
    """
    from api_client import ApiError

    semaphore = asyncio.Semaphore(_ENRICH_CONCURRENCY)

    async def _enrich_one(d: dict) -> dict:
        row = dict(d)
        row["status"] = "—"
        row["page_count"] = "—"
        row["version_number"] = "—"
        row["latest_status"] = None
        async with semaphore:
            try:
                detail = await client.get_document(d["document_id"])
            except ApiError:
                return row
        versions = detail.get("versions", [])
        active = next((v for v in versions if v.get("is_active")), None)
        target = active or (versions[0] if versions else None)
        if target:
            row["status"] = target.get("status", "—")
            row["page_count"] = target.get("page_count", "—")
            row["version_number"] = target.get("version_number", "—")
        if versions:
            row["latest_status"] = versions[0].get("status")
        return row

    return list(await asyncio.gather(*(_enrich_one(d) for d in documents)))


__all__ = [
    "api_for",
    "current_identity",
    "get_templates",
    "login_redirect",
    "page_context",
    "templates",
]
