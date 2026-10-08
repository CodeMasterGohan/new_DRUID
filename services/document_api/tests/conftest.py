"""Shared fixtures: app with SQLite catalog, memory object store, fake queue."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from services.catalog.models import Base, Tenant

from .helpers import signed_headers
from main import create_app
from settings import DocumentApiSettings
from storage import MemoryObjectStore


@pytest.fixture()
def settings():
    return DocumentApiSettings(
        database_url="sqlite://",
        internal_secret="test-internal-secret",
        object_store="memory",
        max_upload_mb=10,
        max_pages_per_document=3,
        redis_url="redis://localhost:6379/0",
        ingest_queue="ingest",
        delete_queue="delete",
    )


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    session.add(Tenant(name="tenant-a"))
    session.add(Tenant(name="tenant-b"))
    session.commit()
    session.close()
    return factory


@pytest.fixture()
def object_store():
    return MemoryObjectStore()


@pytest.fixture()
def enqueued(monkeypatch):
    """Capture RQ enqueue calls instead of touching Redis."""
    calls: list[dict] = []

    def fake_enqueue_rq(queue_name, entrypoint, job, redis_url):
        calls.append(
            {
                "queue_name": queue_name,
                "entrypoint": entrypoint,
                "job_id": str(job.id),
                "job_type": job.job_type,
                "redis_url": redis_url,
            }
        )

    import routes.documents as documents_routes

    monkeypatch.setattr(documents_routes, "enqueue_rq", fake_enqueue_rq)
    return calls


@pytest.fixture()
def app(settings, session_factory, object_store):
    return create_app(
        settings=settings,
        session_factory=session_factory,
        object_store=object_store,
    )


@pytest.fixture()
def client(app):
    # Context manager runs the lifespan, which wires app.state.
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_headers(settings):
    return signed_headers(settings.internal_secret)
