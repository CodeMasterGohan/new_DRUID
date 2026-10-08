"""Catalog seam: tenant isolation + searchable-version hard filter on SQLite.

Thin integration slice over services.catalog (which has its own 17 unit
tests): proves the repository contract a sibling service would rely on —
documents are tenant-scoped, and only READY active versions are searchable,
independent of any index state.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.catalog.activation import activate_version
from services.catalog.errors import NotFoundError
from services.catalog.models import Base, IngestionState, Tenant
from services.catalog.repository import CatalogRepository
from services.common.request_context import RequestContext


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def ctx(group_id, user_id="user-1"):
    return RequestContext(user_id=user_id, group_id=group_id, roles=("reader",))


def seed_tenant(session, name):
    t = Tenant(name=name, is_active=True)
    session.add(t)
    session.flush()
    return t


def seed_ready_doc(session, group_id, parser_revision="pdftext-1.0"):
    """Create a document with one READY active version; return (repo, doc)."""
    from services.catalog.models import ParserRevisionStats

    repo = CatalogRepository(session, ctx(group_id))
    doc = repo.create_document(display_name="report.pdf", content_checksum="abc")
    session.add(ParserRevisionStats(
        parser="native", parser_revision=parser_revision,
        min_chunks_per_page=1.0, max_chunks_per_page=5.0, sample_count=50,
    ))
    session.flush()
    version = repo.create_version(
        doc.id, parser="native", parser_revision=parser_revision, page_count=10
    )
    version.status = IngestionState.INDEXING
    version.chunk_count_actual = 20
    session.flush()
    activate_version(session, ctx(group_id), version.id)
    session.commit()
    return repo, doc


def test_searchable_versions_only_returns_ready_active(session):
    seed_tenant(session, "tenant-a")
    repo, doc = seed_ready_doc(session, "tenant-a")

    rows = session.execute(repo.searchable_versions_query()).scalars().all()
    assert [v.document_id for v in rows] == [doc.id]

    # A second version stuck in PARSING must not appear.
    v2 = repo.create_version(doc.id, parser="native",
                             parser_revision="pdftext-1.0", page_count=10)
    v2.status = IngestionState.PARSING
    session.commit()
    rows = session.execute(repo.searchable_versions_query()).scalars().all()
    assert [v.document_id for v in rows] == [doc.id]


def test_cross_tenant_document_fetch_fails(session):
    seed_tenant(session, "tenant-a")
    seed_tenant(session, "tenant-b")
    repo_a, doc = seed_ready_doc(session, "tenant-a")

    repo_b = CatalogRepository(session, ctx("tenant-b"))
    with pytest.raises(NotFoundError):
        repo_b.get_document(doc.id)
    # ... and tenant-b sees no searchable versions at all.
    assert session.execute(repo_b.searchable_versions_query()).scalars().all() == []
    # Tenant-a is unaffected.
    assert len(session.execute(repo_a.searchable_versions_query()).scalars().all()) == 1
