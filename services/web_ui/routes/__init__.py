"""Shared helpers for web_ui routes."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from api_client import DruidApiClient
from deps import Identity, NotAuthenticated, get_identity

templates: Jinja2Templates | None = None


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
        timeout_seconds=settings.forward_timeout_seconds,
        transport=transport,
    )


def login_redirect(request: Request) -> RedirectResponse:
    nxt = request.url.path
    target = "/login" if nxt in ("", "/") else f"/login?next={nxt}"
    return RedirectResponse(url=target, status_code=303)


def page_context(request: Request, identity: Identity, **extra):
    return {
        "request": request,
        "identity": identity,
        "is_admin": identity.is_admin,
        "can_review": identity.can_review,
        **extra,
    }


async def enrich_documents(client, documents: list[dict]) -> list[dict]:
    """Attach active-version status/page count to each document summary.

    The list endpoint returns summaries only; version state lives on the
    detail endpoint. N+1 is fine for a human-scale table (≤100 rows).
    """
    from api_client import ApiError

    enriched = []
    for d in documents:
        row = dict(d)
        row["status"] = "—"
        row["page_count"] = "—"
        row["version_number"] = "—"
        try:
            detail = await client.get_document(d["document_id"])
        except ApiError:
            enriched.append(row)
            continue
        versions = detail.get("versions", [])
        active = next((v for v in versions if v.get("is_active")), None)
        target = active or (versions[0] if versions else None)
        if target:
            row["status"] = target.get("status", "—")
            row["page_count"] = target.get("page_count", "—")
            row["version_number"] = target.get("version_number", "—")
        enriched.append(row)
    return enriched


__all__ = [
    "api_for",
    "current_identity",
    "get_templates",
    "login_redirect",
    "page_context",
    "templates",
]
