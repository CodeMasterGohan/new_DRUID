"""Trusted request context shared across new_DRUID services.

Implements the ``request_context`` contract from §4 of the architecture spec:

    request_context:
      user_id: "<authenticated-user>"
      group_id: "<authorized-tenant>"
      roles: ["<authorized-role>"]
      request_id: "<trace-id>"

The context may be derived ONLY from validated authentication (the gateway's
JWT validation) or from the gateway's HMAC-signed internal headers, verified
by ``services.gateway.druid_internal.verify_internal_headers``.

It must NEVER be constructed from document metadata, user-supplied tool
arguments, unvalidated HTTP headers, an MCP session ID, or a file's original
path or filename. Every consumer of this type must treat a missing or invalid
context as fail-closed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Immutable trusted identity + tenant context for one request."""

    user_id: str
    group_id: str
    roles: tuple[str, ...] = ()
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if not self.user_id or not self.group_id:
            raise ValueError(
                "RequestContext requires non-empty user_id and group_id; "
                "missing identity must fail closed, never default."
            )
        # Normalize roles to a tuple so the context stays hashable/immutable.
        object.__setattr__(self, "roles", tuple(self.roles or ()))

    def has_role(self, role: str) -> bool:
        """Check whether the authenticated principal holds ``role``."""
        return role in self.roles
