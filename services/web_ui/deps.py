"""Request identity for the web_ui (spec §16, §4).

Identity comes ONLY from the JWT in the session cookie, validated with the
same contract as the gateway. Spoofable sources — ``X-Group-Id`` /
``X-Druid-*`` headers, query params, form fields, route args — are never
consulted. Missing or invalid tokens fail closed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Request

from auth import AuthError, JWTSettings, validate_token

log = logging.getLogger(__name__)

ADMIN_ROLE = "admin"
REVIEWER_ROLE = "reviewer"


@dataclass(frozen=True)
class Identity:
    """Trusted identity for one request, derived from the validated token."""

    user_id: str
    group_id: str
    roles: tuple[str, ...]
    token: str  # the raw JWT, forwarded as-is to the gateway

    @property
    def is_admin(self) -> bool:
        return ADMIN_ROLE in self.roles

    @property
    def can_review(self) -> bool:
        # Admins can always review; the reviewer role is granted out-of-band.
        return self.is_admin or REVIEWER_ROLE in self.roles


class NotAuthenticated(Exception):
    """No usable session cookie — the caller should redirect to /login."""


def get_identity(request: Request, settings: JWTSettings) -> Identity:
    """Build the trusted identity from the session cookie.

    Raises :class:`NotAuthenticated` when the cookie is missing or the
    token fails validation. Never reads tenant identity from headers or
    params — those are attacker-controlled.
    """
    cookie_name = request.app.state.web_ui_settings.cookie_name
    raw = request.cookies.get(cookie_name)
    if not raw:
        raise NotAuthenticated("no session cookie")
    try:
        claims = validate_token(raw, settings)
    except AuthError as exc:
        log.info("invalid session token: %s", exc)
        raise NotAuthenticated(str(exc)) from exc
    return Identity(
        user_id=claims["user_id"],
        group_id=claims["group_id"],
        roles=tuple(claims["roles"]),
        token=raw,
    )


__all__ = [
    "ADMIN_ROLE",
    "REVIEWER_ROLE",
    "Identity",
    "NotAuthenticated",
    "get_identity",
]
