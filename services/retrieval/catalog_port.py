"""Catalog port backed by the real catalog repository (spec §4, §10 step 3).

This adapter MUST use ``CatalogRepository.searchable_versions_query`` and
MUST NOT reimplement the searchability filter. The mapping contract with
ingestion is:

* Milvus ``document_id`` (VarChar) == ``str(Document.id)``
* Milvus ``document_version`` (VarChar) == ``str(DocumentVersion.version_number)``

Ingestion MUST write chunk records with exactly these string encodings or
the hard filter will exclude everything.
"""

from __future__ import annotations

from typing import Any

from services.common.request_context import RequestContext

from .errors import DependencyError
from .interfaces import CatalogSearchPort


class SqlAlchemyCatalogPort(CatalogSearchPort):
    """CatalogSearchPort implemented via the catalog's tenant-scoped repository."""

    def __init__(self, session_factory: Any) -> None:
        """``session_factory`` is a callable returning a SQLAlchemy Session."""
        self._session_factory = session_factory

    def allowed_versions(
        self, context: RequestContext
    ) -> frozenset[tuple[str, str]]:
        try:
            from services.catalog.repository import CatalogRepository
        except ImportError as exc:
            raise DependencyError(
                "services.catalog is not installed; cannot enforce the "
                "catalog hard filter"
            ) from exc
        session = self._session_factory()
        try:
            repo = CatalogRepository(session, context)
            versions = repo.list_searchable_versions()
        except Exception as exc:
            raise DependencyError(f"catalog hard-filter query failed: {exc}") from exc
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()
        return frozenset(
            (str(v.document_id), str(v.version_number)) for v in versions
        )
