"""Behavioral regressions for the independent 028d47b audit."""
import hashlib
import uuid
import pytest
from sqlalchemy import select, func
from services.catalog.models import Document, IngestionAdmission, Job, IngestionState
from services.catalog.repository import CatalogRepository
from services.common.request_context import RequestContext
from .helpers import make_pdf, signed_headers
pytestmark = pytest.mark.usefixtures("enqueued")


def upload(client, headers, pages=1):
    return client.post('/v1/documents/upload?page_count=1', headers=headers,
                       files={'file': ('a.pdf', make_pdf(pages), 'application/pdf')})


def register(client, headers, data):
    return client.post('/v1/documents/register', headers=headers, json={
        'filename': 'a.pdf', 'mime_type': 'application/pdf',
        'file_size_bytes': len(data), 'content_sha256': hashlib.sha256(data).hexdigest()})


def redeem(client, headers, ticket, data):
    return client.post('/v1/documents/'+ticket['document_id']+'/bytes', headers=headers,
                      data={'upload_ticket': ticket['upload_ticket']},
                      files={'file': ('a.pdf', data, 'application/pdf')})


def test_denied_acl_hides_list_detail_status_and_job(client, auth_headers, settings,
                                                    session_factory, object_store, monkeypatch):
    result = upload(client, auth_headers).json()
    did = uuid.UUID(result['document_id'])
    with session_factory() as session:
        repo = CatalogRepository(session, RequestContext(user_id='user-1', group_id='tenant-a'))
        repo.grant(did, principal_type='user', principal_id='user-1', permission='read')
        session.commit()
    denied = signed_headers(settings.internal_secret, user_id='other')
    def no_sign(*args, **kwargs):
        pytest.fail('unauthorized response attempted to sign a download')
    monkeypatch.setattr(object_store, 'presigned_get_url', no_sign)
    assert client.get('/v1/documents', headers=denied).json()['documents'] == []
    for path in (f'/v1/documents/{did}', f'/v1/documents/{did}/status',
                 '/v1/jobs/'+result['job_id']):
        assert client.get(path, headers=denied).status_code == 404
    assert len(client.get('/v1/documents', headers=auth_headers).json()['documents']) == 1


def test_actual_pages_not_client_count_charge_quota(client, auth_headers, settings, session_factory):
    settings.quota_ingest_pages_per_day = 3
    assert upload(client, auth_headers, 2).status_code == 201
    rejected = upload(client, auth_headers, 2)
    assert rejected.status_code == 429
    assert int(rejected.headers['retry-after']) > 0
    assert upload(client, auth_headers, 1).status_code == 201
    with session_factory() as session:
        assert session.scalar(select(func.sum(IngestionAdmission.pages))) == 3
        assert session.scalar(select(func.count()).select_from(Document)) == 2


def test_two_phase_is_admitted_once_and_shares_budget(client, auth_headers, settings, session_factory):
    settings.quota_ingest_pages_per_day = 2
    data = make_pdf(2)
    response = register(client, auth_headers, data)
    assert response.status_code < 300, response.text
    ticket = response.json()
    assert redeem(client, auth_headers, ticket, data).status_code == 200
    assert redeem(client, auth_headers, ticket, data).status_code == 409
    assert upload(client, auth_headers).status_code == 429
    with session_factory() as session:
        assert session.scalar(select(func.sum(IngestionAdmission.pages))) == 2


def test_storage_failure_rolls_back_admission(client, auth_headers, settings, object_store,
                                             session_factory, monkeypatch):
    settings.quota_ingest_pages_per_day = 1
    def broken(*a, **k):
        raise RuntimeError('injected storage failure')
    with monkeypatch.context() as m:
        m.setattr(object_store, 'put_file', broken)
        assert upload(client, auth_headers).status_code == 500
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(IngestionAdmission)) == 0
    assert upload(client, auth_headers).status_code == 201


def test_enqueue_retry_does_not_double_charge(client, auth_headers, settings, session_factory, monkeypatch):
    import routes.documents as routes
    settings.quota_ingest_pages_per_day = 1
    data = make_pdf()
    ticket = register(client, auth_headers, data).json()
    with monkeypatch.context() as m:
        def broken(*a, **k): raise RuntimeError('broker unavailable')
        m.setattr(routes, 'enqueue_rq', broken)
        assert redeem(client, auth_headers, ticket, data).status_code == 500
    assert redeem(client, auth_headers, ticket, data).status_code == 200
    with session_factory() as session:
        assert session.scalar(select(func.sum(IngestionAdmission.pages))) == 1


def test_retry_commits_before_worker_and_does_not_overwrite_progress(client, auth_headers,
                                                                  session_factory, monkeypatch):
    import routes.documents as routes
    result = upload(client, auth_headers).json()
    jid = uuid.UUID(result['job_id'])
    with session_factory() as session:
        job = session.get(Job, jid)
        job.state = IngestionState.FAILED
        session.commit()
    def eager_worker(queue, entrypoint, job, redis_url):
        with session_factory() as session:
            current = session.get(Job, job.id)
            assert current.state == IngestionState.UPLOADED
            current.state = IngestionState.READY
            session.commit()
    monkeypatch.setattr(routes, 'enqueue_rq', eager_worker)
    response = client.post(f'/v1/jobs/{jid}/retry', headers=auth_headers)
    assert response.status_code == 200, response.text
    with session_factory() as session:
        assert session.get(Job, jid).state == IngestionState.READY
