"""Catalog-layer exceptions.

All failures are explicit: missing authentication, tenant context, or
authorization raises rather than falling back to unrestricted access (§4,
"fail closed").
"""


class CatalogError(Exception):
    """Base class for all catalog errors."""


class TenantContextError(CatalogError):
    """Raised when the trusted request context is missing or invalid."""


class AuthorizationError(CatalogError):
    """Raised when the context's tenant is not authorized for the operation."""


class NotFoundError(CatalogError):
    """Raised when a tenant-scoped record does not exist (or is not visible)."""


class ConflictError(CatalogError):
    """Raised on idempotency-key conflicts and other uniqueness violations."""


class ActivationError(CatalogError):
    """Raised when a document version cannot be activated (§12)."""
