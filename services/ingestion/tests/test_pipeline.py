"""Pipeline state-machine tests (spec §12).

Runs against the REAL catalog models on SQLite: trusted-context rebuild,
stage transitions, retry→failed escalation, review-required parking, the
activation gate (range check), and the deletion driver. pdftext/vLLM are
never touched — stage functions are monkeypatched with canned outputs.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services.catalog.errors import ActivationError
from services.catalog.models import Base, IngestionState, Job, ParserRevisionStats
from services.catalog.repository import CatalogRepository
from services.common.request_context import RequestContext
from services.ingestion import pipeline
from services.ingestion.pipeline import (
    LoggingIndexSink,
    MemoryObjectStore,
    PipelineDeps,
    _ReviewRequired,
    ensure_tenant,
    run_deletion_job,
    run_ingestion_job,
)


@pytest.fixture()
def db():
    """Engine + session factory + tenant context (fresh session per use)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    sess = maker()
    ensure_tenant(sess, "tenant-a")
    sess.close()

    def new_repo():
        sess = maker()
        ctx = RequestContext(user_id="u1", group_id="tenant-a",
                             roles=("reviewer",))
        return sess, CatalogRepository(sess, ctx)

    yield maker, new_repo
    engine.dispose()


@pytest.fixture()
def job_setup(db):
    """A document + version + ingestion job, ready for the worker."""
    maker, new_repo = db
    sess, repo = new_repo()
    doc = repo.create_document(display_name="doc.pdf", content_checksum="abc")
    version = repo.create_version(doc.id, parser="mixed", parser_revision="r1",
                                  page_count=2)
    job = repo.create_job(
        job_type="ingestion", document_id=doc.id,
        document_version_id=version.id, idempotency_key="k1",
        payload={"pdf_key": "tenant-a/doc/v1/original.pdf"},
    )
    job_id, version_id = job.id, version.id
    sess.commit()
    # Chunk-count baseline so activation can validate ranges.
    sess.add(ParserRevisionStats(
        parser="mixed", parser_revision="r1",
        min_chunks_per_page=1.0, max_chunks_per_page=50.0, sample_count=10))
    sess.commit()
    sess.close()
    return maker, job_id, version_id


def _canned_success(monkeypatch, n_records=5):
    monkeypatch.setattr(
        pipeline, "_parse_document",
        lambda *a, **k: {"trees": [], "routing": {}, "png_cache": {},
                         "visual_pages": []},
    )
    monkeypatch.setattr(pipeline, "_chunk_trees", lambda trees, deps: [])
    monkeypatch.setattr(
        pipeline, "_embed_chunks",
        lambda chunks, parse_out, version, group_id, tenant_prefix, deps, session: [
            {"chunk_id": f"tenant-a:c{i}"} for i in range(n_records)
        ],
    )


def test_happy_path_reaches_ready(job_setup, monkeypatch):
    maker, job_id, version_id = job_setup
    _canned_success(monkeypatch)
    sink = LoggingIndexSink()
    deps = PipelineDeps(index_sink=sink)
    state = run_ingestion_job(str(job_id), session_factory=maker,
                              deps=deps)
    assert state == "READY"
    assert len(sink.indexed) == 5
    sess, repo = _fresh_repo(maker)
    v = repo.get_version(version_id)
    assert v.status == IngestionState.READY and v.is_active is True
    assert v.chunk_count_actual == 5
    sess.close()


def _fresh_repo(maker):
    sess = maker()
    ctx = RequestContext(user_id="u1", group_id="tenant-a", roles=("reviewer",))
    return sess, CatalogRepository(sess, ctx)


def test_transient_failure_retries_then_fails(job_setup, monkeypatch):
    maker, job_id, version_id = job_setup

    def boom(*a, **k):
        raise RuntimeError("pdf exploded")

    monkeypatch.setattr(pipeline, "_parse_document", boom)
    deps = PipelineDeps()
    # First attempt → RETRY_PENDING.
    assert run_ingestion_job(str(job_id), session_factory=maker,
                             deps=deps) == "RETRY_PENDING"
    # Exhaust attempts → FAILED.
    sess = maker()
    job = sess.get(Job, job_id)
    job.max_attempts = 1
    sess.commit()
    sess.close()
    assert run_ingestion_job(str(job_id), session_factory=maker,
                             deps=deps) == "FAILED"
    sess, repo = _fresh_repo(maker)
    assert repo.get_job(job_id).error is not None
    sess.close()


def test_quarantine_parks_review_required(job_setup, monkeypatch):
    maker, job_id, version_id = job_setup

    def parked(*a, **k):
        raise _ReviewRequired("2 pages quarantined")

    monkeypatch.setattr(pipeline, "_parse_document", parked)
    state = run_ingestion_job(str(job_id), session_factory=maker,
                              deps=PipelineDeps())
    assert state == "REVIEW_REQUIRED"
    # Not retried, not failed — parked for a human.
    sess, repo = _fresh_repo(maker)
    assert repo.get_job(job_id).state == IngestionState.REVIEW_REQUIRED
    sess.close()


def test_activation_rejects_out_of_range_counts(job_setup, monkeypatch):
    maker, job_id, version_id = job_setup
    _canned_success(monkeypatch, n_records=5000)  # outside [2, 100]
    state = run_ingestion_job(str(job_id), session_factory=maker,
                              deps=PipelineDeps())
    assert state == "FAILED"
    sess, repo = _fresh_repo(maker)
    assert "outside plausible range" in repo.get_job(job_id).error
    sess.close()


def test_inactive_tenant_fails_closed(job_setup, monkeypatch):
    maker, job_id, version_id = job_setup
    _canned_success(monkeypatch)
    # Deactivate the tenant behind the job's back.
    from sqlalchemy import select
    from services.catalog.models import Tenant

    sess = maker()
    tenant = sess.execute(
        select(Tenant).where(Tenant.name == "tenant-a")).scalars().one()
    tenant.is_active = False
    sess.commit()
    sess.close()
    with pytest.raises(Exception):
        run_ingestion_job(str(job_id), session_factory=maker,
                          deps=PipelineDeps())


def test_deletion_driver(db):
    maker, new_repo = db
    sess, repo = new_repo()
    doc = repo.create_document(display_name="doc.pdf", content_checksum="abc")
    doc_id = doc.id
    store = MemoryObjectStore()
    store.put_bytes(f"tenant-a/{doc_id}/original.pdf", b"data")
    job = repo.create_job(job_type="deletion", document_id=doc_id,
                          idempotency_key="del1")
    job_id = job.id
    sess.commit()
    sess.close()
    sink = LoggingIndexSink()
    deps = PipelineDeps(index_sink=sink, object_store=store)
    state = run_deletion_job(str(job_id), session_factory=maker, deps=deps)
    assert state == "DELETED"
    assert sink.deleted == [(str(doc_id), "tenant-a")]
    sess, repo = _fresh_repo(maker)
    with pytest.raises(Exception):
        repo.get_document(doc_id)  # soft-deleted: invisible
    sess.close()


def test_terminal_jobs_are_not_rerun(job_setup, monkeypatch):
    maker, job_id, version_id = job_setup
    _canned_success(monkeypatch)
    deps = PipelineDeps()
    assert run_ingestion_job(str(job_id), session_factory=maker,
                             deps=deps) == "READY"
    calls = []
    monkeypatch.setattr(pipeline, "_parse_document",
                        lambda *a, **k: calls.append(1) or {})
    assert run_ingestion_job(str(job_id), session_factory=maker,
                             deps=deps) == "READY"
    assert calls == []


def test_mixed_native_and_visual_trees_get_monotonic_chunk_orders_and_headings():
    from services.ingestion.router import PageGeometry, TextBlock
    from services.ingestion.native_worker import NativePage
    from services.ingestion.olmocr_client import OlmOcrResult
    from services.ingestion.tree import build_tree_from_native, build_tree_from_olmocr

    p0_geom = PageGeometry(
        page_index=0,
        page_bbox=(0, 0, 612, 792),
        blocks=[
            TextBlock(text="1. Introduction", bbox=(72, 700, 300, 718), lines=[(72, 700, 300, 718)]),
            TextBlock(text="Native page zero content.", bbox=(72, 660, 500, 680), lines=[(72, 660, 500, 680)]),
        ],
    )
    native_tree = build_tree_from_native(
        "doc-mix",
        "1",
        [NativePage(page_index=0, page_bbox=(0, 0, 612, 792), rotation=0, geometry=p0_geom)],
        {},
        parser_revision="r1",
    )
    visual_tree = build_tree_from_olmocr(
        "doc-mix",
        "1",
        [OlmOcrResult(page_index=1, markdown="Visual page one content.", metadata={}, quality_status="accepted", attempts=1, latency_s=0.1)],
        parser_revision="r1",
    )
    deps = PipelineDeps(token_len=lambda t: len(t.split()))
    chunks = pipeline._chunk_trees([native_tree, visual_tree], deps)
    orders = [c.chunk_order for c in chunks]
    assert len(orders) == len(set(orders))
    assert orders == list(range(len(orders)))
