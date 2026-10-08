"""Trusted request context for the new_DRUID MCP server.

Implements §4 of the architecture spec: tenant identity is derived ONLY from
validated authentication (or the explicit stdio credential context) and is
carried in a context-local value. It is NEVER read from tool arguments,
document metadata, HTTP headers, MCP session IDs, or file paths.

Missing or invalid context fails closed: :func:`get_context` raises instead
of returning a permissive default.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Validated identity for one tool invocation / HTTP request."""

    user_id: str
    group_id: str  # the authorized tenant
    roles: frozenset[str]
    request_id: str  # generated server-side; never accepted from the caller

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles & frozenset(roles))


class MissingContextError(RuntimeError):
    """Raised when a tool runs without a validated request context."""


_current: ContextVar[RequestContext | None] = ContextVar(
    "druid_request_context", default=None
)


def get_context() -> RequestContext:
    """Return the validated context for this invocation, or fail closed."""
    ctx = _current.get()
    if ctx is None:
        raise MissingContextError(
            "No validated request context is present. Refusing to proceed."
        )
    return ctx


def set_context(ctx: RequestContext) -> Token:
    return _current.set(ctx)


def clear_context(token: Token) -> None:
    _current.reset(token)


def new_request_id() -> str:
    return uuid.uuid4().hex
