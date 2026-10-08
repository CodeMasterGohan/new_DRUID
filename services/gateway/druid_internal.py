"""Internal identity propagation between gateway and backend services.

Spec §4: "The gateway validates tokens, strips spoofable identity headers,
and forwards identity only over an authenticated internal connection.
Internal services must either validate the original token or verify the
gateway's protected context."

This module implements the "gateway's protected context": the gateway signs
the trusted context with HMAC-SHA256 (``INTERNAL_SECRET``) and sets it
as ``X-Druid-*`` headers. Backend services verify the signature with
:func:`verify_internal_headers` before trusting the context. The bearer
token is also forwarded so services may validate the original token instead.

Header contract (for sibling services implementing the other side):

* ``X-Druid-User-Id`` — authenticated user id
* ``X-Druid-Group-Id`` — authorized tenant
* ``X-Druid-Roles`` — comma-separated roles (may be empty)
* ``X-Druid-Request-Id`` — trace id for the request
* ``X-Druid-Internal-Sig`` — ``v1:<hex-hmac-sha256>`` over the canonical
  string ``"druid-v1|{user_id}|{group_id}|{roles_csv}|{request_id}"``
"""

from __future__ import annotations

import hmac
from hashlib import sha256

from services.common.request_context import RequestContext

from .auth import AuthError

HEADER_USER = "X-Druid-User-Id"
HEADER_GROUP = "X-Druid-Group-Id"
HEADER_ROLES = "X-Druid-Roles"
HEADER_REQUEST_ID = "X-Druid-Request-Id"
HEADER_SIG = "X-Druid-Internal-Sig"

_SIGNING_VERSION = "v1"

#: Headers a client must never be able to smuggle identity through. The
#: gateway strips these from every inbound request (lowercased for
#: case-insensitive comparison) and never reads identity from them.
SPOOFABLE_HEADERS = frozenset(
    {
        "x-user-id",
        "x-userid",
        "x-group-id",
        "x-groupid",
        "x-tenant-id",
        "x-tenantid",
        "x-tenant",
        "x-roles",
        "x-role",
        "x-forwarded-user",
        "x-forwarded-groups",
        "x-auth-user",
        "x-authenticated-userid",
        "x-remote-user",
        "x-mcp-session-id",
        # Our own internal headers must never arrive from a client either.
        "x-druid-user-id",
        "x-druid-group-id",
        "x-druid-roles",
        "x-druid-request-id",
        "x-druid-internal-sig",
    }
)


def _canonical_string(context: RequestContext) -> str:
    roles_csv = ",".join(sorted(context.roles))
    return (
        f"druid-{_SIGNING_VERSION}|{context.user_id}|{context.group_id}"
        f"|{roles_csv}|{context.request_id}"
    )


def sign_context(context: RequestContext, secret: str) -> str:
    """Return the ``X-Druid-Internal-Sig`` value for a trusted context."""
    if not secret:
        raise AuthError("internal service secret is not configured")
    digest = hmac.new(
        secret.encode("utf-8"), _canonical_string(context).encode("utf-8"), sha256
    ).hexdigest()
    return f"{_SIGNING_VERSION}:{digest}"


def build_internal_headers(context: RequestContext, secret: str) -> dict[str, str]:
    """Build the full set of internal identity headers for forwarding."""
    return {
        HEADER_USER: context.user_id,
        HEADER_GROUP: context.group_id,
        HEADER_ROLES: ",".join(sorted(context.roles)),
        HEADER_REQUEST_ID: context.request_id,
        HEADER_SIG: sign_context(context, secret),
    }


def verify_internal_headers(headers, secret: str) -> RequestContext:
    """Verify gateway-signed internal headers and rebuild the context.

    ``headers`` is any case-insensitive mapping (e.g. Starlette headers).
    Raises :class:`AuthError` when any header is missing or the signature
    does not match — backend services must fail closed here.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    user_id = lowered.get(HEADER_USER.lower())
    group_id = lowered.get(HEADER_GROUP.lower())
    roles_csv = lowered.get(HEADER_ROLES.lower(), "")
    request_id = lowered.get(HEADER_REQUEST_ID.lower())
    signature = lowered.get(HEADER_SIG.lower())
    if not all([user_id, group_id, request_id, signature]):
        raise AuthError("incomplete internal identity headers")
    context = RequestContext(
        user_id=user_id,
        group_id=group_id,
        roles=tuple(r for r in roles_csv.split(",") if r),
        request_id=request_id,
    )
    expected = sign_context(context, secret)
    if not hmac.compare_digest(expected, signature):
        raise AuthError("internal identity signature mismatch")
    return context


def strip_spoofable_headers(headers: dict) -> int:
    """Remove spoofable identity headers from a mutable header dict.

    Returns the number of headers removed. Keys are matched
    case-insensitively.
    """
    doomed = [k for k in headers if k.lower() in SPOOFABLE_HEADERS]
    for key in doomed:
        del headers[key]
    return len(doomed)


__all__ = [
    "HEADER_GROUP",
    "HEADER_REQUEST_ID",
    "HEADER_ROLES",
    "HEADER_SIG",
    "HEADER_USER",
    "SPOOFABLE_HEADERS",
    "build_internal_headers",
    "sign_context",
    "strip_spoofable_headers",
    "verify_internal_headers",
]
