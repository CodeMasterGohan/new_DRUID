"""FastAPI dependencies: settings, DB sessions, trusted context (spec §4)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import lru_cache

from fastapi import Depends, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from services.catalog.repository import CatalogRepository
from services.common.request_context import RequestContext

from internal_auth import trusted_context_from_request
from settings import DocumentApiSettings
from storage import MinIOObjectStore, ObjectStore


@lru_cache(maxsize=1)
def get_settings() -> DocumentApiSettings:
    """Process-wide settings (override in tests via dependency_overrides)."""
    return DocumentApiSettings()


def session_factory_from_settings(
    settings: DocumentApiSettings,
) -> Callable[[], Session]:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)


def default_object_store(settings: DocumentApiSettings) -> ObjectStore:
    from storage import MemoryObjectStore

    if settings.object_store == "memory":
        return MemoryObjectStore()
    return MinIOObjectStore(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket=settings.minio_bucket,
        secure=settings.minio_secure,
    )


def get_session_factory(request: Request) -> Callable[[], Session]:
    return request.app.state.session_factory


def get_session(
    factory: Callable[[], Session] = Depends(get_session_factory),
) -> Iterator[Session]:
    """Yield a DB session per request; always closed afterwards."""
    session = factory()
    try:
        yield session
    finally:
        session.close()


async def get_trusted_context(
    request: Request,
    settings: DocumentApiSettings = Depends(get_settings),
) -> RequestContext:
    """Build the trusted context from gateway-signed headers or fail closed.

    Identity comes ONLY from the verified ``X-Druid-*`` headers (§4). There
    is no body/query-param fallback — a request without valid headers is a
    401, never an anonymous or default-tenant request.
    """
    return await trusted_context_from_request(request, settings.internal_secret)


def get_repository(
    session: Session = Depends(get_session),
    context: RequestContext = Depends(get_trusted_context),
) -> CatalogRepository:
    return CatalogRepository(session, context)


def get_object_store(request: Request) -> ObjectStore:
    return request.app.state.object_store


__all__ = [
    "default_object_store",
    "get_object_store",
    "get_repository",
    "get_session",
    "get_session_factory",
    "get_settings",
    "get_trusted_context",
    "session_factory_from_settings",
]
