"""Opt-in upload → RQ → activation → search against disposable real services.

Inference uses explicit deterministic test doubles; this is not a model-quality
evaluation. See docs/markdown-uploads.md for the service variables and command.
"""

import hashlib
import os
import uuid
from pathlib import Path

import pytest


@pytest.mark.skipif(os.getenv('DRUID_TEST_MARKDOWN_LIVE') != '1', reason='opt-in live services')
def test_markdown_upload_queue_index_search(monkeypatch):
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient
    from minio import Minio
    from pymilvus import MilvusClient
    from redis import Redis
    from rq import Queue, SimpleWorker
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import sessionmaker

    from main import create_app
    from settings import DocumentApiSettings
    from storage import RustFSObjectStore
    from services.catalog.baselines import import_baseline
    from services.catalog.models import Tenant, Job, IngestionState
    from services.catalog.repository import CatalogRepository
    from services.common.request_context import RequestContext
    from services.ingestion import pipeline
    from services.ingestion.config import IngestionConfig
    from services.ingestion.milvus_sink import MilvusIndexSink
    from services.ingestion.tree import build_tree_from_markdown
    from services.milvus_setup.schema import build_pymilvus_schema, build_index_params
    from services.retrieval.predicates import AuthPredicate
    from services.retrieval.production import MilvusVectorStore
    from .helpers import signed_headers

    name = 'markdown_' + uuid.uuid4().hex
    bucket = name.replace('_', '-')
    admin = create_engine(os.environ['DRUID_TEST_DATABASE_URL'])
    url = make_url(os.environ['DRUID_TEST_DATABASE_URL']).update_query_dict(
        {'options': f'-csearch_path={name}'})
    monkeypatch.setenv('DATABASE_URL', url.render_as_string(hide_password=False))
    monkeypatch.setenv('DRUID_AUTH_MODE', 'legacy')
    engine = create_engine(url)
    maker = sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(os.environ['DRUID_TEST_REDIS_URL'])
    queue = Queue(name, connection=redis)
    milvus = MilvusClient(uri=os.environ['DRUID_TEST_MILVUS_URI'])
    endpoint = os.environ['DRUID_TEST_S3_ENDPOINT']
    access = os.environ['DRUID_TEST_S3_ACCESS_KEY']
    secret = os.environ['DRUID_TEST_S3_SECRET_KEY']
    s3 = Minio(endpoint, access_key=access, secret_key=secret, secure=False)
    data = (Path(os.environ['DRUID_TEST_MARKDOWN_PATH']).read_bytes()
            if os.getenv('DRUID_TEST_MARKDOWN_PATH') else
            b'# GitLab CI\n\nUse include:local to reuse pipeline configuration.\n')
    store = RustFSObjectStore(endpoint, access, secret, bucket)
    deps = pipeline.PipelineDeps(
        config=IngestionConfig(processing_mode='cpu'), object_store=store,
        index_sink=MilvusIndexSink(uri=os.environ['DRUID_TEST_MILVUS_URI'],
                                  collection=name, client=milvus),
        token_len=lambda value: len(value.split()))
    # Keep the real RQ entry point and every pipeline stage. Only model/tokenizer
    # inference is replaced by the explicit PipelineDeps test defaults.
    monkeypatch.setattr(pipeline, 'build_production_deps', lambda: deps)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA {name}'))
    try:
        root = Path(__file__).resolve().parents[3]
        config = Config(str(root / 'services/catalog/alembic.ini'))
        config.set_main_option('script_location', str(root / 'services/catalog/alembic'))
        command.upgrade(config, 'head')
        s3.make_bucket(bucket)
        milvus.create_collection(name, schema=build_pymilvus_schema(),
                                 index_params=build_index_params())
        milvus.load_collection(name)
        count = len(pipeline._chunk_trees([
            build_tree_from_markdown('measurement', '1', data.decode('utf-8-sig'))], deps))
        with maker() as session:
            session.add_all([Tenant(name='tenant-a'), Tenant(name='tenant-b')])
            # Measured with this fixture tokenizer, never a production baseline.
            import_baseline(session, {'parser': 'markdown', 'parser_revision': 'markdown-v1',
                'samples': [{'sha256': hashlib.sha256(data).hexdigest(), 'pages': 1, 'chunks': count}]})
            session.commit()
        settings = DocumentApiSettings(database_url=str(url), internal_secret='live-test-only-secret',
            redis_url=os.environ['DRUID_TEST_REDIS_URL'], ingest_queue=name,
            druid_auth_mode='legacy')
        app = create_app(settings=settings, session_factory=maker, object_store=store)
        headers = signed_headers(settings.internal_secret)
        foreign = signed_headers(settings.internal_secret, group_id='tenant-b')
        with TestClient(app) as client:
            for flow in ('direct', 'ticket'):
                files = {'file': ('gitlab.md', data, 'text/markdown')}
                if flow == 'direct':
                    response = client.post('/v1/documents/upload', headers=headers, files=files)
                    assert response.status_code == 201, response.text
                else:
                    registered = client.post('/v1/documents/register', headers=headers, json={
                        'filename': 'gitlab.md', 'mime_type': 'text/markdown',
                        'file_size_bytes': len(data), 'content_sha256': hashlib.sha256(data).hexdigest()})
                    assert registered.status_code == 201, registered.text
                    body = registered.json()
                    response = client.post(f"/v1/documents/{body['document_id']}/bytes",
                        headers=headers, files=files, data={'upload_ticket': body['upload_ticket']})
                    assert response.status_code == 200, response.text
                uploaded = response.json()
                job_id = uuid.UUID(uploaded['job_id'])
                with maker() as session:
                    job = session.get(Job, job_id)
                    assert store.get_bytes(job.payload['pdf_key']) == data
                SimpleWorker([queue], connection=redis).work(
                    burst=True, max_jobs=1, logging_level='WARNING')
                with maker() as session:
                    job = session.get(Job, job_id)
                    assert job.state == IngestionState.READY, job.error
                    repo = CatalogRepository(session, RequestContext(user_id='user-1', group_id='tenant-a'))
                    allowed = frozenset((str(v.document_id), str(v.version_number))
                                       for v in repo.list_searchable_versions())
                    assert any(doc == uploaded['document_id'] for doc, _ in allowed)
                    prefix = f'tenant-a/{job.document_id}/{job.document_version_id}/trees'
                    assert store.get_bytes(f'{prefix}/markdown.json')
                milvus.flush(name)
                search = MilvusVectorStore(uri=os.environ['DRUID_TEST_MILVUS_URI'],
                                          collection=name, client=milvus)
                hits = search.bm25_search('include', AuthPredicate('tenant-a', allowed), 100)
                assert any(hit.record.document_id == uploaded['document_id'] for hit in hits)
                assert search.bm25_search('include', AuthPredicate('tenant-b', allowed), 100) == []
                assert client.get(f"/v1/jobs/{job_id}", headers=foreign).status_code == 404
                print(f'PASS {flow}: {len(data)} bytes uploaded; READY; {count} chunks; '
                      'BM25 finds GitLab content; foreign tenant blocked')
    finally:
        queue.delete(delete_jobs=True)
        redis.close()
        if milvus.has_collection(name):
            milvus.drop_collection(name)
        milvus.close()
        if s3.bucket_exists(bucket):
            for obj in s3.list_objects(bucket, recursive=True):
                s3.remove_object(bucket, obj.object_name)
            s3.remove_bucket(bucket)
        # Workers cache a separate engine by database URL.
        cached = pipeline._SESSION_MAKERS.pop(url.render_as_string(hide_password=False), None)
        if cached is not None:
            cached.kw['bind'].dispose()
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA {name} CASCADE'))
        admin.dispose()
