"""Login / logout (spec §16: Auth).

There is no credential store in new_DRUID — access tokens are provisioned
out-of-band by the operator (HS256 JWT with ``sub`` / ``group_id`` /
``roles`` claims, signed with the shared JWT secret). The login page
accepts a pasted token, validates it with the gateway's contract, and
stores it in an HttpOnly/Secure/SameSite cookie.

Roles are NEVER accepted from the login form: they come from the validated
token claims only. There is no UI path that grants ``admin``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from auth import AuthError, validate_token
from deps import NotAuthenticated

from . import current_identity, get_templates

log = logging.getLogger(__name__)
router = APIRouter()


def _safe_next(target: str) -> str:
    """Accept only local absolute paths; reject protocol-relative redirects."""
    if (not isinstance(target, str) or not target.startswith("/")
            or target.startswith("//") or "\\\\" in target
            or any(ord(char) < 32 or ord(char) == 127 for char in target)):
        return "/documents"
    from urllib.parse import urlsplit
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.path.startswith("//"):
        return "/documents"
    return target


@router.get("/login")
def login_form(request: Request, next: str = "/documents"):
    next = _safe_next(next)
    try:
        current_identity(request)
        return RedirectResponse(url=next, status_code=303)
    except NotAuthenticated:
        pass
    return get_templates().TemplateResponse(
        "login.html", {"request": request, "next": next, "error": None}
    )


@router.post("/login")
def login_submit(
    request: Request,
    token: str = Form(...),
    next: str = Form(default="/documents"),
):
    """Validate the pasted token and establish the session cookie."""
    next = _safe_next(next)
    token = (token or "").strip()
    try:
        validate_token(token, request.app.state.jwt_settings)
    except AuthError as exc:
        log.info("login rejected: %s", exc)
        return get_templates().TemplateResponse(
            "login.html",
            {"request": request, "next": next, "error": f"invalid token: {exc}"},
            status_code=401,
        )
    settings = request.app.state.web_ui_settings
    response = RedirectResponse(url=next, status_code=303)
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.cookie_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )
    return response


@router.post("/logout")
def logout(request: Request):
    settings = request.app.state.web_ui_settings
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key=settings.cookie_name)
    return response


__all__ = ["router"]
