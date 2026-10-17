"""Authoritative PostgreSQL group RBAC for Keycloak-subject users.

JWT scopes/realm roles are NEVER group entitlements. Only DB memberships
(and the explicitly bootstrapped system-admin identity) grant DRUID access.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DruidUser, GroupMembership, Tenant

GROUP_ROLES = frozenset({"member", "manager"})


class IdentityDenied(PermissionError):
    pass


@dataclass(frozen=True)
class ActiveIdentity:
    subject: str
    group_id: str
    roles: tuple[str, ...]


def get_user(session: Session, subject: str) -> DruidUser:
    if not subject or len(subject) > 255:
        raise IdentityDenied("invalid OIDC subject")
    user = session.scalar(select(DruidUser).where(DruidUser.subject == subject))
    if user is None or not user.is_active:
        raise IdentityDenied("user not provisioned or disabled")
    return user


def memberships(session: Session, user: DruidUser) -> list[tuple[Tenant, GroupMembership]]:
    return list(session.execute(
        select(Tenant, GroupMembership)
        .join(GroupMembership, GroupMembership.tenant_id == Tenant.id)
        .where(GroupMembership.user_id == user.id, Tenant.is_active.is_(True))
        .order_by(Tenant.name)
    ).all())


def resolve(session: Session, subject: str, selected: str | None = None) -> ActiveIdentity:
    """Resolve a caller-selected group only after membership checks."""
    user = get_user(session, subject)
    rows = memberships(session, user)
    selected_row = next((m for tenant, m in rows if tenant.name == selected), None)
    if selected:
        if selected_row is None and not user.is_system_admin:
            raise IdentityDenied("not a member of requested group")
        if selected_row is None and session.scalar(
            select(Tenant).where(Tenant.name == selected, Tenant.is_active.is_(True))
        ) is None:
            raise IdentityDenied("group unavailable")
        group_name = selected
    elif rows:
        group_name = rows[0][0].name
        selected_row = rows[0][1]
    elif user.is_system_admin:
        group_name = "__system__"  # only global admin routes use this context
    else:
        raise IdentityDenied("no active group memberships")
    roles = ["user", "member"]
    if selected_row is not None and selected_row.role == "manager":
        roles += ["reviewer", "group_admin"]
    if user.is_system_admin:
        roles += ["admin", "reviewer", "group_admin"]
    return ActiveIdentity(subject, group_name, tuple(sorted(set(roles))))


def provision_first_admin(session: Session, subject: str, expected_subject: str) -> DruidUser:
    """One-time bootstrap requires valid Keycloak JWT AND exact configured sub.

    Stops granting any bootstrap privileges after the first admin exists.
    Existing users can subsequently be managed only using the admin API.
    """
    if not expected_subject or subject != expected_subject:
        raise IdentityDenied("user not provisioned")
    existing_admin = session.scalar(select(DruidUser).where(DruidUser.is_system_admin.is_(True)))
    if existing_admin is not None:
        raise IdentityDenied("bootstrap already completed")
    from uuid import uuid4
    user = session.scalar(select(DruidUser).where(DruidUser.subject == subject))
    if user is None:
        user = DruidUser(id=uuid4(), subject=subject, username="initial-admin")
        session.add(user)
    user.is_active = True
    user.is_system_admin = True
    session.flush()
    return user
