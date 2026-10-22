"""Unit tests for the document catalog (spec §4, §5, §12).

Run from the build root:  python -m pytest services/catalog/tests -q
Uses SQLite; PostgreSQL-specific behavior (JSONB, native enums) is covered
by the Alembic migration, which targets PostgreSQL.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.catalog.activation import activate_version, get_expected_chunk_range
from services.catalog.errors import (
    ActivationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    TenantContextError,
)
from services.catalog.models import (
    Base,
    DocumentVersion,
    IngestionState,
    ParserRevisionStats,
    Tenant,
)
from services.catalog.repository import CatalogRepository
from services.common.request_context import RequestContext


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def make_tenant(session, name="tenant-a"):
    tenant = Tenant(name=name, is_active=True)
    session.add(tenant)
    session.flush()
    return tenant


def make_context(group_id="tenant-a", user_id="user-1", roles=("reader",)):
    return RequestContext(user_id=user_id, group_id=group_id, roles=roles)


def make_doc_with_version(session, repo, **version_kwargs):
    doc = repo.create_document(display_name="report.pdf", content_checksum="abc123")
    version_kwargs.setdefault("parser", "native")
    version_kwargs.setdefault("parser_revision", "pdftext-1.0")
    version_kwargs.setdefault("page_count", 10)
    version = repo.create_version(doc.id, **version_kwargs)
    return doc, version


def record_stats(session, parser="native", parser_revision="pdftext-1.0",
                 lo=1.0, hi=5.0):
    stats = ParserRevisionStats(
        parser=parser, parser_revision=parser_revision,
        min_chunks_per_page=lo, max_chunks_per_page=hi, sample_count=50,
    )
    session.add(stats)
    session.flush()
    return stats


# ---------------------------------------------------------------------------
# §4: tenant context enforcement
# ---------------------------------------------------------------------------

def test_repository_requires_trusted_context(session):
    with pytest.raises(TenantContextError):
        CatalogRepository(session, None)
    with pytest.raises((TenantContextError, ValueError)):
        CatalogRepository(session, RequestContext(user_id="u", group_id=""))


def test_create_document_ignores_caller_supplied_tenant_id(session):
    make_tenant(session, "tenant-a")
    repo = CatalogRepository(session, make_context("tenant-a"))
    # A caller-supplied tenant_id must never affect the write (§4 safe writes).
    doc = repo.create_document(
        display_name="evil.pdf", content_checksum="x", tenant_id="evil-tenant"
    )
    assert doc.tenant_id == repo._tenant_id()
    other = CatalogRepository(session, make_context("tenant-a"))
    assert other.get_document(doc.id).id == doc.id


def test_cross_tenant_isolation(session):
    make_tenant(session, "tenant-a")
    make_tenant(session, "tenant-b")
    repo_a = CatalogRepository(session, make_context("tenant-a"))
    doc = repo_a.create_document(display_name="a.pdf", content_checksum="x")

    repo_b = CatalogRepository(session, make_context("tenant-b"))
    with pytest.raises(NotFoundError):
        repo_b.get_document(doc.id)
    assert repo_b.list_documents() == []
    assert repo_b.list_searchable_versions() == []


def test_unknown_or_inactive_tenant_fails_closed(session):
    make_tenant(session, "tenant-a")
    repo = CatalogRepository(session, make_context("ghost-tenant"))
    with pytest.raises(AuthorizationError):
        repo.list_documents()


# ---------------------------------------------------------------------------
# §12: version activation
# ---------------------------------------------------------------------------

def test_activation_happy_path(session):
    make_tenant(session)
    repo = CatalogRepository(session, make_context())
    doc, v1 = make_doc_with_version(session, repo)
    record_stats(session)
    repo.update_version_status(v1.id, IngestionState.INDEXING, chunk_count_actual=30)

    activated = activate_version(session, make_context(), v1.id)
    assert activated.status == IngestionState.READY
    assert activated.is_active is True
    assert activated.activated_at is not None
    session.refresh(doc)
    assert doc.active_version_id == v1.id

    # Activating v2 deactivates v1: never two active versions.
    v2 = repo.create_version(doc.id, parser="native", parser_revision="pdftext-1.0",
                             page_count=10)
    repo.update_version_status(v2.id, IngestionState.INDEXING, chunk_count_actual=40)
    activate_version(session, make_context(), v2.id)
    session.refresh(v1)
    assert v1.is_active is False
    session.refresh(doc)
    assert doc.active_version_id == v2.id


def test_activation_rejects_out_of_range_chunk_count(session):
    make_tenant(session)
    repo = CatalogRepository(session, make_context())
    doc, v = make_doc_with_version(session, repo)
    record_stats(session, lo=1.0, hi=5.0)  # 10 pages -> [10, 50]
    repo.update_version_status(v.id, IngestionState.INDEXING, chunk_count_actual=500)
    with pytest.raises(ActivationError, match="outside plausible range"):
        activate_version(session, make_context(), v.id)
    session.refresh(v)
    assert v.status == IngestionState.INDEXING  # untouched
    assert v.is_active is False


def test_activation_requires_fully_indexed_version(session):
    make_tenant(session)
    repo = CatalogRepository(session, make_context())
    doc, v = make_doc_with_version(session, repo)
    record_stats(session)
    repo.update_version_status(v.id, IngestionState.PARSING, chunk_count_actual=30)
    with pytest.raises(ActivationError, match="not INDEXING"):
        activate_version(session, make_context(), v.id)


def test_activation_requires_chunk_count_baseline(session):
    make_tenant(session)
    repo = CatalogRepository(session, make_context())
    doc, v = make_doc_with_version(session, repo)
    # No ParserRevisionStats recorded -> fail closed, do not guess.
    repo.update_version_status(v.id, IngestionState.INDEXING, chunk_count_actual=30)
    with pytest.raises(ActivationError, match="no chunk-count baseline"):
        activate_version(session, make_context(), v.id)


def test_activation_blocked_by_pending_quarantine(session):
    make_tenant(session)
    ctx = make_context(roles=("reader", "reviewer"))
    repo = CatalogRepository(session, ctx)
    doc, v = make_doc_with_version(session, repo)
    record_stats(session)
    repo.update_version_status(v.id, IngestionState.INDEXING, chunk_count_actual=30)
    repo.quarantine_page(
        document_id=doc.id, document_version_id=v.id, job_id=None,
        page_index=3, page_render_ref="tenant-a/doc/page-3.png",
        parse_attempts={"native": {"ok": False}},
        routing_diagnostics={"text_coverage": 0.12},
    )
    with pytest.raises(ActivationError, match="unreviewed"):
        activate_version(session, ctx, v.id)

    items = repo.list_quarantine()
    repo.resolve_quarantine_item(items[0].id, decision="rejected",
                                 reviewed_by="user-1")
    # Still blocked: version is REVIEW_REQUIRED after quarantine, not INDEXING.
    with pytest.raises(ActivationError):
        activate_version(session, ctx, v.id)


def test_expected_chunk_range_math(session):
    record_stats(session, lo=2.0, hi=4.0)
    assert get_expected_chunk_range(
        session, parser="native", parser_revision="pdftext-1.0", page_count=10
    ) == (20, 40)


# ---------------------------------------------------------------------------
# §10 step 3 / §12: the searchability hard filter
# ---------------------------------------------------------------------------

def test_searchable_versions_hard_filter(session):
    make_tenant(session, "tenant-a")
    make_tenant(session, "tenant-b")
    repo_a = CatalogRepository(session, make_context("tenant-a"))

    doc_ok, v_ok = make_doc_with_version(session, repo_a)
    repo_a.update_version_status(v_ok.id, IngestionState.READY)
    v_ok.is_active = True

    doc_indexing, v_indexing = make_doc_with_version(session, repo_a)
    repo_a.update_version_status(v_indexing.id, IngestionState.INDEXING)
    v_indexing.is_active = True  # active but not READY -> hidden

    doc_inactive, v_inactive = make_doc_with_version(session, repo_a)
    repo_a.update_version_status(v_inactive.id, IngestionState.READY)
    # is_active False -> hidden

    doc_deleting, v_deleting = make_doc_with_version(session, repo_a)
    repo_a.update_version_status(v_deleting.id, IngestionState.DELETING)
    v_deleting.is_active = True  # deleting -> hidden even though "active"

    doc_gone, v_gone = make_doc_with_version(session, repo_a)
    repo_a.update_version_status(v_gone.id, IngestionState.READY)
    v_gone.is_active = True
    repo_a.soft_delete_document(doc_gone.id)  # deleted doc -> hidden

    repo_b = CatalogRepository(session, make_context("tenant-b"))
    doc_b, v_b = make_doc_with_version(session, repo_b)
    repo_b.update_version_status(v_b.id, IngestionState.READY)
    v_b.is_active = True  # other tenant -> hidden from tenant-a

    session.flush()
    visible = {v.id for v in repo_a.list_searchable_versions()}
    assert visible == {v_ok.id}


# ---------------------------------------------------------------------------
# §12: jobs — idempotency and persisted trusted context
# ---------------------------------------------------------------------------

def test_job_idempotency_key_is_per_tenant_unique(session):
    make_tenant(session, "tenant-a")
    make_tenant(session, "tenant-b")
    repo_a = CatalogRepository(session, make_context("tenant-a"))
    job = repo_a.create_job(job_type="ingestion", idempotency_key="up-1")
    assert job.trusted_user_id == "user-1"
    assert job.trusted_group_id == "tenant-a"
    assert job.trusted_roles == ["reader"]
    with pytest.raises(ConflictError):
        repo_a.create_job(job_type="ingestion", idempotency_key="up-1")
    # Same key in another tenant is fine.
    repo_b = CatalogRepository(session, make_context("tenant-b"))
    repo_b.create_job(job_type="ingestion", idempotency_key="up-1")


def test_job_context_revalidation_fails_closed(session):
    tenant = make_tenant(session, "tenant-a")
    repo = CatalogRepository(session, make_context("tenant-a"))
    job = repo.create_job(job_type="ingestion", idempotency_key="up-9")
    assert repo.revalidate_job_context(job) is True
    tenant.is_active = False
    session.flush()
    with pytest.raises(AuthorizationError):
        repo.revalidate_job_context(job)


# ---------------------------------------------------------------------------
# §5: review quarantine
# ---------------------------------------------------------------------------

def test_quarantine_resolve_requires_reviewer_role(session):
    make_tenant(session)
    repo_plain = CatalogRepository(session, make_context(roles=("reader",)))
    doc, v = make_doc_with_version(session, repo_plain)
    item = repo_plain.quarantine_page(
        document_id=doc.id, document_version_id=v.id, job_id=None,
        page_index=0, page_render_ref="r.png", parse_attempts={},
        routing_diagnostics={},
    )
    with pytest.raises(AuthorizationError):
        repo_plain.resolve_quarantine_item(
            item.id, decision="accepted", reviewed_by="user-1"
        )
    repo_reviewer = CatalogRepository(
        session, make_context(roles=("reader", "reviewer"))
    )
    resolved = repo_reviewer.resolve_quarantine_item(
        item.id, decision="accepted", reviewed_by="user-1",
        detail={"chosen_parse": "visual"},
    )
    assert resolved.status == "accepted"
    assert resolved.reviewed_by == "user-1"


def test_quarantine_resolve_allowed_for_admin_role(session):
    make_tenant(session, "tenant-a")
    repo_plain = CatalogRepository(session, make_context(roles=("reader",)))
    doc, v = make_doc_with_version(session, repo_plain)
    item = repo_plain.quarantine_page(
        document_id=doc.id, document_version_id=v.id, job_id=None,
        page_index=0, page_render_ref="r.png", parse_attempts={},
        routing_diagnostics={},
    )
    repo_admin = CatalogRepository(
        session, make_context(roles=("admin",))
    )
    resolved = repo_admin.resolve_quarantine_item(
        item.id, decision="accepted", reviewed_by="admin-user",
        detail={"chosen_parse": "fast"},
    )
    assert resolved.status == "accepted"
    assert resolved.reviewed_by == "admin-user"


def test_quarantine_item_is_tenant_scoped(session):
    make_tenant(session, "tenant-a")
    make_tenant(session, "tenant-b")
    repo_a = CatalogRepository(session, make_context("tenant-a"))
    doc, v = make_doc_with_version(session, repo_a)
    item = repo_a.quarantine_page(
        document_id=doc.id, document_version_id=v.id, job_id=None,
        page_index=0, page_render_ref="r.png", parse_attempts={},
        routing_diagnostics={},
    )
    repo_b = CatalogRepository(session, make_context("tenant-b"))
    with pytest.raises(NotFoundError):
        repo_b.get_quarantine_item(item.id)
    assert repo_b.list_quarantine() == []


# ---------------------------------------------------------------------------
# §4: ACLs — group-wide default, enforced when rows exist
# ---------------------------------------------------------------------------

def test_acl_group_wide_default_and_enforcement(session):
    make_tenant(session, "tenant-a")
    repo = CatalogRepository(session, make_context("tenant-a", user_id="alice"))
    doc = repo.create_document(display_name="d.pdf", content_checksum="x")
    # No ACL rows: every tenant member may read.
    assert repo.check_permission(doc.id, "read") is True
    assert repo.check_permission(doc.id, "write") is False

    repo.grant(doc.id, principal_type="user", principal_id="alice",
               permission="read")
    assert repo.check_permission(doc.id, "read") is True
    # Another tenant member with no grant is now denied (rows exist).
    repo_bob = CatalogRepository(session, make_context("tenant-a", user_id="bob"))
    assert repo_bob.check_permission(doc.id, "read") is False


def test_searchable_versions_enforces_document_acl_in_sql(session):
    make_tenant(session, "tenant-a")
    repo_alice = CatalogRepository(session, make_context("tenant-a", user_id="alice"))
    doc, v = make_doc_with_version(session, repo_alice)
    v.status = IngestionState.READY
    v.is_active = True
    doc.active_version_id = v.id
    session.flush()
    # Default: no ACL rows -> visible to both alice and bob.
    repo_bob = CatalogRepository(session, make_context("tenant-a", user_id="bob"))
    assert len(repo_alice.list_searchable_versions()) == 1
    assert len(repo_bob.list_searchable_versions()) == 1

    # Restrict read ACL to alice only -> bob gets 0 searchable versions from SQL.
    repo_alice.grant(doc.id, principal_type="user", principal_id="alice", permission="read")
    assert len(repo_alice.list_searchable_versions()) == 1
    assert repo_bob.list_searchable_versions() == []
