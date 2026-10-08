"""Caller-context validation (spec §4). Missing/invalid context fails closed."""

from __future__ import annotations

from typing import Any

from services.common.request_context import RequestContext

from .errors import ContextValidationError


def validate_context(context: Any) -> RequestContext:
    """Return the trusted context or raise :class:`ContextValidationError`.

    The context must be a :class:`RequestContext` built by the gateway from
    validated authentication — never from document metadata, tool arguments,
    unvalidated headers, an MCP session ID, or a filename. Anything else
    fails closed: no context, no data, no default tenant.
    """
    if not isinstance(context, RequestContext):
        raise ContextValidationError(
            f"invalid caller context type {type(context).__name__!r}; "
            "expected services.common.request_context.RequestContext"
        )
    if not context.user_id or not context.group_id:
        raise ContextValidationError(
            "caller context has empty user_id or group_id; failing closed"
        )
    return context
