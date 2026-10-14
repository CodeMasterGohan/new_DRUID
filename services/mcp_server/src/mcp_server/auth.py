"""Token validation and tool-level role scopes (§4, §11 transport security).

- :class:`TokenVerifier` is the abstract boundary: any credential mechanism
  can back it.
- :class:`JWTVerifier` validates signature, issuer, audience, and expiry and
  extracts subject / tenant / roles from claims. Every failure raises
  :class:`AuthError` (fail closed).
- :data:`TOOL_ROLES` maps each MCP tool to the roles allowed to invoke it.
  ``resolve_review_item`` and ``get_review_item`` require the reviewer role;
  everything else requires any authenticated role.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import jwt

from .context import RequestContext, new_request_id


class AuthError(Exception):
    """Any authentication/authorization failure. Always fails closed."""


@dataclass(frozen=True, slots=True)
class TokenClaims:
    subject: str
    group_id: str
    roles: frozenset[str] = field(default_factory=frozenset)


class TokenVerifier(ABC):
    """Abstract credential boundary. Implementations must fail closed."""

    @abstractmethod
    def verify(self, token: str) -> TokenClaims:
        """Validate ``token`` and return its claims, or raise :class:`AuthError`."""


class JWTVerifier(TokenVerifier):
    """Verify signed JWTs (HS256 shared secret or RS256 public key)."""

    def __init__(
        self,
        *,
        key: str | bytes,
        algorithms: tuple[str, ...] = ("HS256",),
        issuer: str,
        audience: str,
        leeway_seconds: int = 30,
        subject_claim: str = "sub",
        tenant_claim: str = "grp",
        roles_claim: str = "roles",
    ) -> None:
        if not issuer or not audience:
            raise ValueError("issuer and audience are required")
        self._key = key
        self._algorithms = list(algorithms)
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway_seconds
        self._subject_claim = subject_claim
        self._tenant_claim = tenant_claim
        self._roles_claim = roles_claim

    def verify(self, token: str) -> TokenClaims:
        if not token or not token.strip():
            raise AuthError("empty token")
        try:
            payload = jwt.decode(
                token,
                self._key,
                algorithms=self._algorithms,
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"token validation failed: {exc}") from exc

        subject = payload.get(self._subject_claim)
        group_id = payload.get(self._tenant_claim)
        roles = payload.get(self._roles_claim, [])
        if not subject or not isinstance(subject, str):
            raise AuthError("token missing subject claim")
        if not group_id or not isinstance(group_id, str):
            raise AuthError("token missing tenant claim")
        if not isinstance(roles, (list, tuple)) or not all(
            isinstance(r, str) for r in roles
        ):
            raise AuthError("token roles claim malformed")
        if not roles:
            raise AuthError("token carries no roles")
        return TokenClaims(
            subject=subject, group_id=group_id, roles=frozenset(roles)
        )


def context_from_claims(claims: TokenClaims) -> RequestContext:
    """Build a request context from validated claims.

    The request_id is generated server-side. Nothing about identity is taken
    from caller-controlled values.
    """
    return RequestContext(
        user_id=claims.subject,
        group_id=claims.group_id,
        roles=claims.roles,
        request_id=new_request_id(),
    )


# Tool name -> roles permitted to invoke it. "admin" is a superset role that
# is accepted everywhere a narrower role is. "user", "member", and "reader" are
# accepted for read tools; "user" and "member" are accepted for mutations.
_READ_ROLES = frozenset({"member", "user", "reader", "reviewer", "admin"})
_MUTATION_ROLES = frozenset({"member", "user", "reviewer", "admin"})
_REVIEW_ROLES = frozenset({"reviewer", "admin"})

TOOL_ROLES: dict[str, frozenset[str]] = {
    "search_knowledge_base": _READ_ROLES,
    "ingest_document": _MUTATION_ROLES,
    "get_ingestion_status": _READ_ROLES,
    "list_documents": _READ_ROLES,
    "get_document": _READ_ROLES,
    "get_review_item": _REVIEW_ROLES,
    "resolve_review_item": _REVIEW_ROLES,
    "delete_document": _MUTATION_ROLES,
}


class ToolAccessDenied(AuthError):
    """Caller lacks the role required for the requested tool."""


def check_tool_role(tool_name: str, ctx: RequestContext) -> None:
    allowed = TOOL_ROLES.get(tool_name)
    if allowed is None:
        raise ToolAccessDenied(f"unknown tool: {tool_name}")
    if not (ctx.roles & allowed or "admin" in ctx.roles):
        raise ToolAccessDenied(
            f"tool '{tool_name}' requires one of roles {sorted(allowed)}"
        )
