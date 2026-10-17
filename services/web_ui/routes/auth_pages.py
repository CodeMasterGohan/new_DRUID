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
    """Allow only same-origin absolute paths, including after URL decoding."""
    from urllib.parse import unquote, urlsplit

    if not isinstance(target, str):
        return "/documents"
    candidate = target
    for _ in range(3):
        if (not candidate.startswith("/") or candidate.startswith("//")
                or chr(92) in candidate
                or any(ord(char) < 32 or ord(char) == 127 for char in candidate)):
            return "/documents"
        parsed = urlsplit(candidate)
        if parsed.scheme or parsed.netloc or parsed.path.startswith("//"):
            return "/documents"
        decoded = unquote(candidate)
        if decoded == candidate:
            return target
        candidate = decoded
    return "/documents"


@router.get("/login")
def login_form(request: Request, next: str = "/documents"):
    next = _safe_next(next)
    if request.app.state.web_ui_settings.druid_auth_mode == "keycloak":
        from urllib.parse import quote
        return RedirectResponse(url="/auth/oidc/start?next=" + quote(next, safe=""),
                                status_code=303)
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
    """Legacy paste-token mode only. Keycloak requires proper OIDC callback."""
    if request.app.state.web_ui_settings.druid_auth_mode == "keycloak":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="token-paste login disabled")
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
