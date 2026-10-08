"""JWT validation for the gateway (spec §4: "Transport security").

Validates token signature, issuer, audience, and expiry. Supports HS256
(shared secret, the self-hosted default) and RS256 via a JWKS URL.

The ``group_id`` (tenant) claim is required and must be present in the token
itself — it is never read from headers, tool arguments, or any other
unvalidated source. Missing or invalid tokens fail closed with
:class:`AuthError`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import jwt as pyjwt


class AuthError(Exception):
    """Raised when authentication fails for any reason (fail closed)."""


@dataclass
class JWTSettings:
    secret: str | None = None  # HS256 shared secret
    algorithms: tuple[str, ...] = ("HS256",)
    issuer: str | None = None
    audience: str | None = None
    jwks_url: str | None = None
    jwks_cache_ttl_seconds: int = 3600


def settings_from_env(env=None) -> JWTSettings:
    """Build JWT settings from environment variables.

    Names follow the shared ``.env.example``: ``JWT_SECRET``,
    ``JWT_ALGORITHMS``, ``JWT_ISSUER``, ``JWT_AUDIENCE``, ``JWKS_URL``.
    """
    import os

    env = env or os.environ
    algorithms = tuple(
        a.strip().upper()
        for a in env.get("JWT_ALGORITHMS", "HS256").split(",")
        if a.strip()
    )
    return JWTSettings(
        secret=env.get("JWT_SECRET"),
        algorithms=algorithms or ("HS256",),
        issuer=env.get("JWT_ISSUER"),
        audience=env.get("JWT_AUDIENCE"),
        jwks_url=env.get("JWKS_URL"),
    )


# Small in-process JWKS cache: {jwks_url: (fetched_at, {kid: key})}.
_jwks_cache: dict[str, tuple[float, dict[str, object]]] = {}


def _get_key(token: str, settings: JWTSettings):
    """Resolve the verification key for ``token``'s algorithm."""
    header = pyjwt.get_unverified_header(token)
    alg = str(header.get("alg", "")).upper()
    if alg not in settings.algorithms:
        raise AuthError(f"token algorithm {alg!r} is not allowlisted")

    if alg.startswith("HS"):
        if not settings.secret:
            raise AuthError("HS256 token presented but no DRUID_JWT_SECRET is configured")
        return settings.secret

    # RS256/ES256 path: resolve the key from the configured JWKS URL.
    if not settings.jwks_url:
        raise AuthError(f"{alg} token presented but no DRUID_JWT_JWKS_URL is configured")
    kid = header.get("kid")
    now = time.time()
    cached = _jwks_cache.get(settings.jwks_url)
    if cached is None or now - cached[0] > settings.jwks_cache_ttl_seconds:
        import httpx

        try:
            response = httpx.get(settings.jwks_url, timeout=10.0)
            response.raise_for_status()
            jwks = response.json()
        except Exception as exc:
            raise AuthError(f"could not fetch JWKS: {exc}") from exc
        keys = {}
        for jwk in jwks.get("keys", []):
            try:
                keys[jwk["kid"]] = pyjwt.algorithms.RSAAlgorithm.from_jwk(jwk)
            except Exception:
                continue
        _jwks_cache[settings.jwks_url] = (now, keys)
        cached = (now, keys)
    key = cached[1].get(kid)
    if key is None:
        raise AuthError("token key id is not in the JWKS set")
    return key


def validate_token(raw_token: str, settings: JWTSettings) -> dict:
    """Validate a bearer token and return the trusted identity claims.

    Returns ``{"user_id", "group_id", "roles"}``. Raises :class:`AuthError`
    on expiry, bad signature, wrong issuer/audience, disallowed algorithm,
    or missing required claims.
    """
    if not raw_token:
        raise AuthError("missing bearer token")
    try:
        key = _get_key(raw_token, settings)
        claims = pyjwt.decode(
            raw_token,
            key,
            algorithms=list(settings.algorithms),
            issuer=settings.issuer,
            audience=settings.audience,
            options={
                "require": ["exp", "iss", "aud", "sub"],
                "verify_exp": True,
                "verify_iss": settings.issuer is not None,
                "verify_aud": settings.audience is not None,
            },
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise AuthError("token has expired") from exc
    except pyjwt.InvalidIssuerError as exc:
        raise AuthError("token issuer is not trusted") from exc
    except pyjwt.InvalidAudienceError as exc:
        raise AuthError("token audience does not match this service") from exc
    except pyjwt.InvalidSignatureError as exc:
        raise AuthError("token signature is invalid") from exc
    except pyjwt.PyJWTError as exc:
        raise AuthError(f"token is invalid: {exc}") from exc

    group_id = claims.get("group_id")
    if not group_id or not isinstance(group_id, str):
        # Never fall back to headers or other sources for tenant identity.
        raise AuthError("token is missing the required group_id claim")
    roles = claims.get("roles", [])
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise AuthError("token roles claim must be a list of strings")
    return {
        "user_id": claims["sub"],
        "group_id": group_id,
        "roles": tuple(roles),
    }


__all__ = ["AuthError", "JWTSettings", "settings_from_env", "validate_token"]
