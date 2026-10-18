"""Group and user administration UI: thin forms over the same REST API.

No elevated business logic lives in Jinja/HTML: every mutation uses the
authenticated gateway API, which enforces database-backed RBAC.
"""
from __future__ import annotations

import time

import jwt
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from api_client import ApiError, DruidApiClient
from deps import NotAuthenticated
from . import current_identity, api_for, page_context, get_templates, login_redirect

router = APIRouter()


def _csrf(request: Request, user: str) -> str:
    secret = request.app.state.web_ui_settings.oidc_session_secret
    if not secret:
        raise HTTPException(503, "group management requires OIDC session secret")
    return jwt.encode({"sub": user, "exp": int(time.time()) + 1800},
                      secret, algorithm="HS256")


def _verify(request: Request, subject: str, token: str) -> None:
    secret = request.app.state.web_ui_settings.oidc_session_secret
    try:
        data = jwt.decode(token, secret, algorithms=["HS256"],
                          options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        raise HTTPException(403, "invalid CSRF token")
    if data["sub"] != subject:
        raise HTTPException(403, "CSRF identity mismatch")


def _client_unselected(request: Request, identity):
    cfg = request.app.state.web_ui_settings
    return DruidApiClient(cfg.gateway_url, identity.token,
                          cfg.forward_timeout_seconds,
                          request.app.state.api_transport)


@router.get("/groups")
async def groups(request: Request):
    try:
        identity = current_identity(request)
    except NotAuthenticated:
        return login_redirect(request)
    # The unselected identity endpoint remains usable when a former group
    # membership was revoked while its stale selection cookie still exists.
    api = _client_unselected(request, identity)
    try:
        me = await api.identity()
        is_admin = bool(me["is_system_admin"])
        group_list = (await api.admin_list_groups())["groups"] if is_admin else []
        users = (await api.admin_list_users())["users"] if is_admin else []
        members = {}
        if is_admin:
            for group in group_list:
                members[group["name"]] = (
                    await api._request("GET", f"/v1/admin/groups/{group['name']}/members")
                )["members"]
    except ApiError as exc:
        raise HTTPException(exc.status_code, "Cannot load group memberships")
    request.state.system_admin = is_admin
    return get_templates().TemplateResponse(
        "groups.html",
        page_context(
            request, identity, me=me, managed_groups=group_list, users=users,
            members=members, csrf=_csrf(request, identity.user_id),
        ),
    )


@router.post("/groups/select")
async def select_group(request: Request, group: str = Form(...), csrf: str = Form(...)):
    identity = current_identity(request)
    _verify(request, identity.user_id, csrf)
    me = await _client_unselected(request, identity).identity()
    available = {g["name"] for g in me["groups"]}
    if me["is_system_admin"]:
        groups = await _client_unselected(request, identity).admin_list_groups()
        available |= {g["name"] for g in groups["groups"] if g["is_active"]}
    if group not in available:
        raise HTTPException(403, "group membership required")
    cfg = request.app.state.web_ui_settings
    resp = RedirectResponse("/documents", status_code=303)
    resp.set_cookie("druid_selected_group", group, httponly=True,
                    secure=cfg.cookie_secure, samesite="Lax")
    return resp


@router.post("/admin/groups/create")
async def create_group(request: Request, name: str = Form(...), csrf: str = Form(...)):
    identity = current_identity(request)
    _verify(request, identity.user_id, csrf)
    try:
        await api_for(identity, request).admin_create_group(name)
    except ApiError as exc:
        raise HTTPException(exc.status_code, "Group creation rejected")
    return RedirectResponse("/groups", status_code=303)


@router.post("/admin/users/create")
async def create_user(request: Request, subject: str = Form(...),
                      username: str = Form(""), email: str = Form(""),
                      csrf: str = Form(...)):
    identity = current_identity(request)
    _verify(request, identity.user_id, csrf)
    try:
        await api_for(identity, request).admin_create_user({
            "subject": subject, "username": username, "email": email or None,
        })
    except ApiError as exc:
        raise HTTPException(exc.status_code, "User creation rejected")
    return RedirectResponse("/groups", status_code=303)


@router.post("/admin/groups/{group}/members")
async def set_membership(request: Request, group: str, subject: str = Form(...),
                         role: str = Form("member"), csrf: str = Form(...)):
    identity = current_identity(request)
    _verify(request, identity.user_id, csrf)
    if role not in ("member", "manager"):
        raise HTTPException(422, "invalid role")
    try:
        await api_for(identity, request).admin_set_member(group, subject, role)
    except ApiError as exc:
        raise HTTPException(exc.status_code, "Membership change rejected")
    return RedirectResponse("/groups", status_code=303)


@router.post("/admin/groups/{group}/members/{subject}/remove")
async def remove_membership(request: Request, group: str, subject: str,
                            csrf: str = Form(...)):
    identity = current_identity(request)
    _verify(request, identity.user_id, csrf)
    try:
        await api_for(identity, request).admin_remove_member(group, subject)
    except ApiError as exc:
        raise HTTPException(exc.status_code, "Membership removal rejected")
    return RedirectResponse("/groups", status_code=303)
