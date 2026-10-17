"""REST-first administration of Keycloak identities and DRUID-owned groups.

All routes require gateway HMAC, with fresh database-admin authorization.
Mutations return JSON and have no browser-only or hidden code paths.
"""
from __future__ import annotations

import re
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from deps import get_session, get_trusted_context
from services.catalog.identity import get_user, memberships
from services.catalog.models import DruidUser, GroupMembership, Tenant
from services.common.request_context import RequestContext

router = APIRouter()
_GROUP_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")


class NewUser(BaseModel):
    subject: str = Field(min_length=1, max_length=255)
    username: str = Field(default="", max_length=255)
    email: str | None = Field(default=None, max_length=320)
    is_system_admin: bool = False


class ChangeUser(BaseModel):
    is_active: bool | None = None
    is_system_admin: bool | None = None
    username: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)


class NewGroup(BaseModel):
    name: str = Field(min_length=2, max_length=63)


class ChangeGroup(BaseModel):
    is_active: bool


class MembershipInput(BaseModel):
    subject: str = Field(min_length=1, max_length=255)
    role: Literal["member", "manager"] = "member"


def _admin(session: Session, ctx: RequestContext) -> DruidUser:
    # Do not authorize from caller-supplied JWT roles or from a stale HMAC
    # header; inspect current user row on every privileged operation.
    user = session.scalar(select(DruidUser).where(DruidUser.subject == ctx.user_id))
    if user is None or not user.is_active or not user.is_system_admin:
        raise HTTPException(status_code=403, detail="system administrator required")
    return user


def _user(session: Session, subject: str) -> DruidUser:
    user = session.scalar(select(DruidUser).where(DruidUser.subject == subject))
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user


def _group(session: Session, name: str) -> Tenant:
    row = session.scalar(select(Tenant).where(Tenant.name == name))
    if row is None:
        raise HTTPException(status_code=404, detail="group not found")
    return row


def _user_json(u: DruidUser) -> dict:
    return {"subject": u.subject, "username": u.username, "email": u.email,
            "is_active": u.is_active, "is_system_admin": u.is_system_admin}


def _group_json(g: Tenant) -> dict:
    return {"name": g.name, "is_active": g.is_active, "group_id": str(g.id)}


@router.get("/v1/identity/me")
def whoami(context: RequestContext = Depends(get_trusted_context),
          session: Session = Depends(get_session)):
    u = _user(session, context.user_id)
    if not u.is_active:
        raise HTTPException(403, "account disabled")
    groups = [
        {"name": group.name, "role": membership.role}
        for group, membership in memberships(session, u)
    ]
    return {**_user_json(u), "selected_group": context.group_id,
            "roles": list(context.roles), "groups": groups}


@router.get("/v1/admin/users")
def list_users(context: RequestContext = Depends(get_trusted_context),
               session: Session = Depends(get_session)):
    _admin(session, context)
    return {"users": [_user_json(u) for u in session.scalars(
        select(DruidUser).order_by(DruidUser.subject)).all()]}


@router.post("/v1/admin/users", status_code=201)
def create_user(data: NewUser, context: RequestContext = Depends(get_trusted_context),
                session: Session = Depends(get_session)):
    _admin(session, context)
    if session.scalar(select(DruidUser).where(DruidUser.subject == data.subject)):
        raise HTTPException(409, "user already exists")
    u = DruidUser(id=uuid.uuid4(), **data.model_dump())
    session.add(u)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "user already exists")
    return _user_json(u)


@router.patch("/v1/admin/users/{subject}")
def update_user(subject: str, data: ChangeUser,
                context: RequestContext = Depends(get_trusted_context),
                session: Session = Depends(get_session)):
    _admin(session, context)
    user = _user(session, subject)
    payload = data.model_dump(exclude_unset=True)
    if user.subject == context.user_id and (
        payload.get("is_active") is False or payload.get("is_system_admin") is False
    ):
        raise HTTPException(409, "cannot deactivate or demote current administrator")
    if (payload.get("is_active") is False or payload.get("is_system_admin") is False) and user.is_system_admin:
        count = session.scalar(select(func.count()).select_from(DruidUser).where(
            DruidUser.is_system_admin.is_(True), DruidUser.is_active.is_(True)))
        if count <= 1:
            raise HTTPException(409, "cannot remove last active administrator")
    for key, value in payload.items():
        setattr(user, key, value)
    session.commit()
    return _user_json(user)


@router.get("/v1/admin/groups")
def list_groups(context: RequestContext = Depends(get_trusted_context),
                session: Session = Depends(get_session)):
    _admin(session, context)
    return {"groups": [_group_json(g) for g in session.scalars(
        select(Tenant).order_by(Tenant.name)).all()]}


@router.post("/v1/admin/groups", status_code=201)
def create_group(data: NewGroup, context: RequestContext = Depends(get_trusted_context),
                 session: Session = Depends(get_session)):
    _admin(session, context)
    if not _GROUP_NAME.fullmatch(data.name):
        raise HTTPException(422, "group name must be lowercase alphanumeric with - or _")
    if session.scalar(select(Tenant).where(Tenant.name == data.name)):
        raise HTTPException(409, "group already exists")
    group = Tenant(id=uuid.uuid4(), name=data.name, is_active=True)
    session.add(group)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "group already exists")
    return _group_json(group)


@router.patch("/v1/admin/groups/{name}")
def update_group(name: str, change: ChangeGroup,
                 context: RequestContext = Depends(get_trusted_context),
                 session: Session = Depends(get_session)):
    _admin(session, context)
    group = _group(session, name)
    group.is_active = change.is_active
    session.commit()
    return _group_json(group)


@router.get("/v1/admin/groups/{name}/members")
def list_members(name: str, context: RequestContext = Depends(get_trusted_context),
                 session: Session = Depends(get_session)):
    _admin(session, context)
    group = _group(session, name)
    users = session.execute(
        select(DruidUser, GroupMembership)
        .join(GroupMembership, GroupMembership.user_id == DruidUser.id)
        .where(GroupMembership.tenant_id == group.id)
        .order_by(DruidUser.subject)
    ).all()
    return {"group": name, "members": [
        {**_user_json(u), "role": m.role} for u, m in users
    ]}


@router.put("/v1/admin/groups/{name}/members/{subject}")
def add_member(name: str, subject: str, body: MembershipInput,
               context: RequestContext = Depends(get_trusted_context),
               session: Session = Depends(get_session)):
    _admin(session, context)
    if body.subject != subject:
        raise HTTPException(422, "subject mismatch")
    group, user = _group(session, name), _user(session, subject)
    row = session.scalar(select(GroupMembership).where(
        GroupMembership.tenant_id == group.id,
        GroupMembership.user_id == user.id,
    ))
    if row is None:
        row = GroupMembership(id=uuid.uuid4(), tenant_id=group.id, user_id=user.id)
        session.add(row)
    row.role = body.role
    session.commit()
    return {"group": name, "subject": subject, "role": row.role}


@router.delete("/v1/admin/groups/{name}/members/{subject}")
def remove_member(name: str, subject: str,
                  context: RequestContext = Depends(get_trusted_context),
                  session: Session = Depends(get_session)):
    _admin(session, context)
    group, user = _group(session, name), _user(session, subject)
    row = session.scalar(select(GroupMembership).where(
        GroupMembership.tenant_id == group.id,
        GroupMembership.user_id == user.id,
    ))
    if row is None:
        raise HTTPException(404, "membership not found")
    session.delete(row)
    session.commit()
    return {"removed": True, "group": name, "subject": subject}
