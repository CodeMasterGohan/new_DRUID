"""Disposable PostgreSQL/Redis integration gates (never production databases)."""
import os
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from sqlalchemy import create_engine, text, select, func
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from alembic.config import Config
from alembic import command
from services.catalog.models import Tenant, IngestionAdmission, Document, Job, IngestionState
from services.catalog.repository import CatalogRepository
from services.catalog.work_lock import document_work_lock
from services.catalog.errors import ConflictError
from services.common.request_context import RequestContext
from services.document_api.ingestion_admission import admit
from services.catalog.baselines import import_baseline
from services.catalog.activation import activate_version


@pytest.fixture
def database(monkeypatch):
    url = os.environ.get('DRUID_TEST_DATABASE_URL')
    if not url:
        pytest.skip('requires disposable DRUID_TEST_DATABASE_URL')
    schema = 'audit_' + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA {schema}'))
    scoped_url = make_url(url).update_query_dict({'options': f'-csearch_path={schema}'})
    monkeypatch.setenv('DATABASE_URL', scoped_url.render_as_string(hide_password=False))
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / 'services/catalog/alembic.ini'))
    config.set_main_option('script_location', str(root / 'services/catalog/alembic'))
    engine = create_engine(scoped_url)
    try:
        command.upgrade(config, 'head')
        command.upgrade(config, 'head')
        maker = sessionmaker(engine, expire_on_commit=False)
        with maker() as session:
            tenant = Tenant(name='integration')
            session.add(tenant)
            session.commit()
            tid = tenant.id
        yield maker, tid
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()


def test_concurrent_admission_never_overdraws(database):
    maker, tid = database
    barrier = threading.Barrier(2)
    def request():
        from fastapi import HTTPException
        with maker() as session:
            barrier.wait(timeout=10)
            try:
                admit(session, tid, uuid.uuid4(), 6, 10)
                session.commit()
                return 201
            except HTTPException as exc:
                session.rollback()
                return exc.status_code
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(request) for _ in range(2)]
        assert sorted(f.result(timeout=20) for f in futures) == [201, 429]
    with maker() as session:
        assert session.scalar(select(func.sum(IngestionAdmission.pages))) == 6


def test_document_lock_survives_commits_and_excludes_other_connections(database):
    maker, _ = database
    did = uuid.uuid4()
    with maker() as owner:
        with document_work_lock(owner, did):
            owner.execute(text('SELECT 1'))
            owner.commit()
            def contender():
                with maker() as session:
                    with pytest.raises(ConflictError):
                        with document_work_lock(session, did):
                            pytest.fail('concurrent owner acquired lock')
            with ThreadPoolExecutor(1) as pool:
                pool.submit(contender).result(timeout=10)
        with maker() as session:
            with document_work_lock(session, did):
                assert session.scalar(text('SELECT 1')) == 1


def test_fresh_migrations_baseline_and_activation(database):
    maker, _ = database
    ctx = RequestContext(user_id='u', group_id='integration')
    with maker() as session:
        repo = CatalogRepository(session, ctx)
        doc = repo.create_document(display_name='corpus.pdf', content_checksum='corpus')
        version = repo.create_version(doc.id, parser='mixed', parser_revision='router-v1', page_count=2)
        repo.update_version_status(version.id, IngestionState.INDEXING, chunk_count_actual=6)
        import_baseline(session, {'parser':'mixed', 'parser_revision':'router-v1',
            'samples':[{'sha256':'a'*64, 'pages':2, 'chunks':6}]})
        activate_version(session, ctx, version.id)
        session.commit()
        assert repo.get_document(doc.id).active_version_id == version.id


def test_real_redis_dispatch_and_quota():
    url = os.environ.get('DRUID_TEST_REDIS_URL')
    if not url:
        pytest.skip('requires disposable DRUID_TEST_REDIS_URL')
    from redis import Redis
    from rq import Queue
    from services.gateway.quotas import RedisQuotaStore, QuotaConfig
    redis = Redis.from_url(url)
    name = 'audit-' + uuid.uuid4().hex
    queue = Queue(name, connection=redis)
    try:
        job = queue.enqueue('services.ingestion.pipeline.run_ingestion_job', str(uuid.uuid4()))
        assert queue.fetch_job(job.id).func_name == 'services.ingestion.pipeline.run_ingestion_job'
        store = RedisQuotaStore(redis, QuotaConfig(query_qps=1))
        assert store.check_query(name).allowed
        assert not store.check_query(name).allowed
        job.delete()
    finally:
        queue.delete(delete_jobs=True)
        redis.delete('druid:quota:query:'+name)
