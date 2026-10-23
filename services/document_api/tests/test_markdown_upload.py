"""Markdown must be accepted through both upload contracts."""
import hashlib

import pytest


MARKDOWN = b'# GitLab CI\n\nUse include:local for reusable pipeline configuration.\n'


def test_upload_markdown(client, auth_headers, enqueued):
    response = client.post('/v1/documents/upload', headers=auth_headers,
        files={'file': ('gitlab.md', MARKDOWN, 'text/markdown')})
    assert response.status_code == 201, response.text
    assert response.json()['page_count'] == 1
    assert len(enqueued) == 1


def test_ticketed_markdown_upload(client, auth_headers, enqueued):
    response = client.post('/v1/documents/register', headers=auth_headers, json={
        'filename': 'gitlab.md', 'mime_type': 'text/markdown',
        'file_size_bytes': len(MARKDOWN), 'content_sha256': hashlib.sha256(MARKDOWN).hexdigest(),
    })
    assert response.status_code == 201, response.text
    body = response.json()
    uploaded = client.post(f"/v1/documents/{body['document_id']}/bytes", headers=auth_headers,
        data={'upload_ticket': body['upload_ticket']},
        files={'file': ('gitlab.md', MARKDOWN, 'text/markdown')})
    assert uploaded.status_code == 200, uploaded.text
    assert len(enqueued) == 1


@pytest.mark.parametrize('mime', ['text/plain', 'application/octet-stream', 'text/markdown; charset=utf-8'])
def test_browser_markdown_mime(client, auth_headers, enqueued, mime):
    response = client.post('/v1/documents/upload', headers=auth_headers,
        files={'file': ('GitLab.MD', MARKDOWN, mime)})
    assert response.status_code == 201, response.text
    assert len(enqueued) == 1


@pytest.mark.parametrize('content', [b' ' * 32, b'# Valid header' + b' ' * 100 + b'\xff',
    b'# Valid header' + b' ' * 100 + b'\x00', b'%PDF-1.7 fake PDF'])
def test_rejects_invalid_markdown(client, auth_headers, enqueued, content):
    response = client.post('/v1/documents/upload', headers=auth_headers,
        files={'file': ('gitlab.md', content, 'text/markdown')})
    assert response.status_code == 400, response.text
    assert enqueued == []


def test_markdown_utf8_split_at_validation_boundary():
    import io
    from services.document_api.validation import validate_upload
    data = b'a' * (1024 * 1024 - 1) + 'é\n'.encode()
    stream = io.BytesIO(data)
    assert validate_upload(stream, data[:16], 'text/markdown', 'gitlab.md') == 'text/markdown'
    assert stream.tell() == 0
    assert stream.read() == data


def test_ticketed_binary_cannot_masquerade_as_markdown(client, auth_headers, enqueued):
    content = MARKDOWN + b'\x00'
    response = client.post('/v1/documents/register', headers=auth_headers, json={
        'filename': 'gitlab.md', 'mime_type': 'text/markdown',
        'file_size_bytes': len(content), 'content_sha256': hashlib.sha256(content).hexdigest()})
    assert response.status_code == 201
    body = response.json()
    uploaded = client.post(f"/v1/documents/{body['document_id']}/bytes", headers=auth_headers,
        data={'upload_ticket': body['upload_ticket']},
        files={'file': ('gitlab.md', content, 'text/markdown')})
    assert uploaded.status_code == 400, uploaded.text
    assert enqueued == []


def test_markdown_requires_extension_at_registration(client, auth_headers, enqueued):
    response = client.post('/v1/documents/register', headers=auth_headers, json={
        'filename': 'gitlab.pdf', 'mime_type': 'text/markdown',
        'file_size_bytes': len(MARKDOWN), 'content_sha256': hashlib.sha256(MARKDOWN).hexdigest()})
    assert response.status_code == 400
    assert enqueued == []


def test_markdown_cannot_borrow_pdf_activation_baseline(
    client, auth_headers, enqueued, session_factory, object_store,
):
    import uuid
    from services.catalog.models import Job, Document, ParserRevisionStats
    from services.ingestion.pipeline import PipelineDeps, run_ingestion_job

    with session_factory() as session:
        session.add(ParserRevisionStats(parser='mixed', parser_revision='router-v1',
            min_chunks_per_page=1, max_chunks_per_page=100, sample_count=1))
        session.commit()
    response = client.post('/v1/documents/upload', headers=auth_headers,
        files={'file': ('gitlab.md', MARKDOWN, 'text/markdown')})
    assert response.status_code == 201, response.text
    job_id = response.json()['job_id']
    deps = PipelineDeps(object_store=object_store, token_len=lambda text: len(text.split()))
    assert run_ingestion_job(job_id, session_factory=session_factory, deps=deps) == 'FAILED'
    with session_factory() as session:
        job = session.get(Job, uuid.UUID(job_id))
        assert "no chunk-count baseline recorded for parser 'markdown'" in job.error
        assert session.get(Document, job.document_id).active_version_id is None
