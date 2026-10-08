"""Verify the gateway's HMAC-signed internal identity headers (spec §4).

This is a byte-exact mirror of the signing contract documented in
``services/gateway/druid_internal.py``:

* Headers: ``X-Druid-User-Id``, ``X-Druid-Group-Id``, ``X-Druid-Roles``
  (comma-separated, may be empty), ``X-Druid-Request-Id``,
  ``X-Druid-Internal-Sig``.
* Canonical string: ``"druid-v1|{user_id}|{group_id}|{roles_csv}|{request_id}"``
  where ``roles_csv`` is the comma-joined *sorted* roles.
* Signature: ``"v1:<hex-hmac-sha256>"`` over the canonical string.

It is kept as a local copy — rather than importing the gateway package —
so this service's image stays independent of the gateway's. The canonical
string must be changed in BOTH places at once; the test-suite verifies
cross-compatibility against the gateway's ``build_internal_headers``.

Fail-closed: missing headers, an unconfigured secret, or a signature
mismatch all raise :class:`InternalAuthError`. There is no fallback to
untrusted identity sources.
"""

from __future__ import annotations

import hmac
from hashlib import sha256

from fastapi import Request

from services.common.request_context import RequestContext

HEADER_USER = "X-Druid-User-Id"
HEADER_GROUP = "X-Druid-Group-Id"
HEADER_ROLES = "X-Druid-Roles"
HEADER_REQUEST_ID = "X-Druid-Request-Id"
HEADER_SIG = "X-Druid-Internal-Sig"

_SIGNING_VERSION = "v1"


class InternalAuthError(Exception):
    """Raised when internal identity headers are missing or invalid."""


def _canonical_string(user_id: str, group_id: str, roles: tuple[str, ...],
                      request_id: str) -> str:
    roles_csv = ",".join(sorted(roles))
    return (
        f"druid-{_SIGNING_VERSION}|{user_id}|{group_id}"
        f"|{roles_csv}|{request_id}"
    )


def _expected_signature(user_id: str, group_id: str, roles: tuple[str, ...],
                        request_id: str, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        _canonical_string(user_id, group_id, roles, request_id).encode("utf-8"),
        sha256,
    ).hexdigest()
    return f"{_SIGNING_VERSION}:{digest}"


def verify_internal_headers(headers, secret: str) -> RequestContext:
    """Verify gateway-signed headers and rebuild the trusted context.

    ``headers`` is any case-insensitive mapping (e.g. Starlette's headers).
    Raises :class:`InternalAuthError` when the secret is unconfigured, any
    header is missing, or the signature does not match.
    """
    if not secret:
        raise InternalAuthError("internal service secret is not configured")
    lowered = {k.lower(): v for k, v in headers.items()}
    user_id = lowered.get(HEADER_USER.lower())
    group_id = lowered.get(HEADER_GROUP.lower())
    roles_csv = lowered.get(HEADER_ROLES.lower(), "")
    request_id = lowered.get(HEADER_REQUEST_ID.lower())
    signature = lowered.get(HEADER_SIG.lower())
    if not all([user_id, group_id, request_id, signature]):
        raise InternalAuthError("incomplete internal identity headers")
    roles = tuple(r for r in roles_csv.split(",") if r)
    expected = _expected_signature(user_id, group_id, roles, request_id, secret)
    if not hmac.compare_digest(expected, signature):
        raise InternalAuthError("internal identity signature mismatch")
    # RequestContext itself rejects empty user_id/group_id (fail closed).
    return RequestContext(
        user_id=user_id,
        group_id=group_id,
        roles=roles,
        request_id=request_id,
    )


async def trusted_context_from_request(request: Request, secret: str) -> RequestContext:
    """FastAPI-friendly wrapper: build the trusted context or raise 401."""
    return verify_internal_headers(request.headers, secret)


__all__ = [
    "HEADER_GROUP",
    "HEADER_REQUEST_ID",
    "HEADER_ROLES",
    "HEADER_SIG",
    "HEADER_USER",
    "InternalAuthError",
    "trusted_context_from_request",
    "verify_internal_headers",
]
