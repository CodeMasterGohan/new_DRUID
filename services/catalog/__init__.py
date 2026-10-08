"""PostgreSQL document catalog for new_DRUID (spec §12).

The catalog is the system of record for tenant ownership, document versions,
job state, and the review quarantine. Every table carrying tenant data has a
``tenant_id`` column, and all access goes through
:class:`services.catalog.repository.CatalogRepository`, which scopes every
query to the trusted request context's tenant (§4).
"""

from .errors import (
    ActivationError,
    AuthorizationError,
    CatalogError,
    ConflictError,
    NotFoundError,
    TenantContextError,
)
from .models import (
    Base,
    Document,
    DocumentACL,
    DocumentVersion,
    IngestionState,
    Job,
    ParserRevisionStats,
    ReviewQuarantineItem,
    Tenant,
)

__all__ = [
    "ActivationError",
    "AuthorizationError",
    "Base",
    "CatalogError",
    "ConflictError",
    "Document",
    "DocumentACL",
    "DocumentVersion",
    "IngestionState",
    "Job",
    "NotFoundError",
    "ParserRevisionStats",
    "ReviewQuarantineItem",
    "Tenant",
    "TenantContextError",
]
