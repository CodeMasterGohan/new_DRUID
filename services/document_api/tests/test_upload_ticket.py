"""Tests for the two-phase upload ticket flow and job-scoped status.

Covers ``POST /v1/documents/register`` (declaration validation, ticket
issuance, catalog + job reservation without enqueue),
``POST /v1/documents/{id}/bytes`` (single-use TTL-bound ticket redemption,
declaration-vs-bytes verification, enqueue), and ``GET /v1/jobs/{job_id}``
(tenant-scoped status, cross-tenant 404).
"""

from __future__ import annotations

import hashlib
import io
import uuid

from .helpers import make_pdf, signed_headers

from deps import get_settings


def _register(client, headers, *, filename="report.pdf",
              mime_type="application/pdf", file_size_bytes=None,
              content_sha256=None):
    pdf = make_pdf(1)
    body = {
        "filename": filename,
        "mime_type": mime_type,
        "file_size_bytes": file_size_bytes if file_size_bytes is not None else len(pdf),
        "content_sha256": content_sha256 or hashlib.sha256(pdf).hexdigest(),
    }
    return client.post("/v1/documents/register", headers=headers, json=body), pdf


def _redeem(client, headers, document_id, ticket, data: bytes,
            filename="report.pdf", content_type="application/pdf"):
    return client.post(
        f"/v1/documents/{document_id}/bytes",
        headers=headers,
        files={"file": (filename, io.BytesIO(data), content_type)},
        data={"upload_ticket": ticket},
    )


# ---------------------------------------------------------------------------
# POST /v1/documents/register
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_requires_auth(self, client):
        resp, _ = _register(client, {})
        assert resp.status_code == 401

    def test_register_happy_path(self, client, auth_headers, enqueued,
                                 object_store):
        resp, pdf = _register(client, auth_headers)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert uuid.UUID(body["document_id"])
        assert uuid.UUID(body["job_id"])
        assert body["upload_ticket"]  # opaque single-use ticket
        assert body["expires_in_seconds"] == 900
        # Reservation only: job exists but nothing is enqueued and no bytes
        # are stored yet.
        assert enqueued == []
        assert list(object_store._data.keys()) == []
        # The reserved job is visible via the job-scoped status endpoint.
        status = client.get(f"/v1/jobs/{body['job_id']}", headers=auth_headers)
        assert status.status_code == 200
        assert status.json()["state"] == "UPLOADED"
        assert status.json()["document_id"] == body["document_id"]

    def test_register_rejects_unsupported_mime(self, client, auth_headers):
        resp, _ = _register(client, auth_headers, mime_type="application/zip")
        assert resp.status_code == 400

    def test_register_rejects_bad_sha256(self, client, auth_headers):
        resp, _ = _register(client, auth_headers, content_sha256="not-hex")
        assert resp.status_code == 400

    def test_register_rejects_zero_size(self, client, auth_headers):
        resp, _ = _register(client, auth_headers, file_size_bytes=0)
        assert resp.status_code == 400

    def test_register_rejects_oversize(self, client, auth_headers):
        # conftest caps uploads at 10 MiB.
        resp, _ = _register(
            client, auth_headers, file_size_bytes=11 * 1024 * 1024
        )
        assert resp.status_code == 413

    def test_register_is_tenant_scoped(self, client, settings, auth_headers):
        headers_b = signed_headers(settings.internal_secret, group_id="tenant-b")
        resp, _ = _register(client, headers_b)
        assert resp.status_code == 201, resp.text
        job_id = resp.json()["job_id"]
        # tenant-a cannot see tenant-b's reserved job.
        assert client.get(f"/v1/jobs/{job_id}",
                          headers=auth_headers).status_code == 404


# ---------------------------------------------------------------------------
# POST /v1/documents/{id}/bytes (ticket redemption)
# ---------------------------------------------------------------------------


class TestRedeem:
    def test_redeem_happy_path(self, client, auth_headers, enqueued,
                               object_store):
        resp, pdf = _register(client, auth_headers)
        reg = resp.json()
        resp = _redeem(client, auth_headers, reg["document_id"],
                       reg["upload_ticket"], pdf)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["document_id"] == reg["document_id"]
        assert body["job_id"] == reg["job_id"]
        assert body["status"] == "UPLOADED"
        assert body["page_count"] == 1
        assert body["checksum_sha256"] == hashlib.sha256(pdf).hexdigest()
        # The registration job became the ingestion job and was enqueued.
        assert len(enqueued) == 1
        assert enqueued[0]["queue_name"] == "ingest"
        assert enqueued[0]["entrypoint"].endswith("run_ingestion_job")
        assert enqueued[0]["job_id"] == reg["job_id"]
        # Bytes stored under the tenant-scoped key.
        keys = list(object_store._data.keys())
        assert len(keys) == 1
        assert keys[0].startswith("tenant-a/")
        assert object_store._data[keys[0]] == pdf

    def test_redeem_requires_auth(self, client, auth_headers):
        reg = _register(client, auth_headers)[0].json()
        resp = _redeem(client, {}, reg["document_id"], reg["upload_ticket"],
                       make_pdf(1))
        assert resp.status_code == 401

    def test_redeem_wrong_ticket_is_403(self, client, auth_headers, enqueued):
        reg = _register(client, auth_headers)[0].json()
        resp = _redeem(client, auth_headers, reg["document_id"],
                       "wrong-ticket", make_pdf(1))
        assert resp.status_code == 403
        assert enqueued == []

    def test_redeem_is_single_use(self, client, auth_headers, enqueued):
        resp, pdf = _register(client, auth_headers)
        reg = resp.json()
        first = _redeem(client, auth_headers, reg["document_id"],
                        reg["upload_ticket"], pdf)
        assert first.status_code == 200, first.text
        second = _redeem(client, auth_headers, reg["document_id"],
                         reg["upload_ticket"], pdf)
        assert second.status_code == 409
        assert len(enqueued) == 1  # no duplicate ingestion job

    def test_redeem_expired_ticket_is_410(self, client, app, auth_headers,
                                          settings):
        # A negative TTL makes the ticket born expired.
        expired_settings = settings.model_copy(
            update={"upload_ticket_ttl_seconds": -1}
        )
        original_override = app.dependency_overrides[get_settings]
        app.dependency_overrides[get_settings] = lambda: expired_settings
        try:
            resp, pdf = _register(client, auth_headers)
            reg = resp.json()
        finally:
            app.dependency_overrides[get_settings] = original_override
        resp = _redeem(client, auth_headers, reg["document_id"],
                       reg["upload_ticket"], pdf)
        assert resp.status_code == 410

    def test_redeem_size_mismatch_is_400(self, client, auth_headers):
        resp, pdf = _register(client, auth_headers)
        reg = resp.json()
        resp = _redeem(client, auth_headers, reg["document_id"],
                       reg["upload_ticket"], pdf + b"extra")
        assert resp.status_code == 400

    def test_redeem_checksum_mismatch_is_400(self, client, auth_headers):
        pdf = make_pdf(1)
        tampered = bytearray(pdf)
        tampered[-10] ^= 0xFF  # same length, different bytes
        assert len(tampered) == len(pdf)
        resp, _ = _register(
            client, auth_headers,
            file_size_bytes=len(tampered),
            content_sha256=hashlib.sha256(pdf).hexdigest(),
        )
        reg = resp.json()
        resp = _redeem(client, auth_headers, reg["document_id"],
                       reg["upload_ticket"], bytes(tampered))
        assert resp.status_code == 400

    def test_redeem_unknown_document_is_404(self, client, auth_headers):
        resp = _redeem(client, auth_headers, str(uuid.uuid4()), "t",
                       make_pdf(1))
        assert resp.status_code == 404

    def test_redeem_other_tenant_is_404(self, client, settings, auth_headers):
        headers_b = signed_headers(settings.internal_secret, group_id="tenant-b")
        reg = _register(client, headers_b)[0].json()
        resp = _redeem(client, auth_headers, reg["document_id"],
                       reg["upload_ticket"], make_pdf(1))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /v1/jobs/{job_id}
# ---------------------------------------------------------------------------


class TestJobStatus:
    def test_job_status_requires_auth(self, client):
        resp = client.get(f"/v1/jobs/{uuid.uuid4()}")
        assert resp.status_code == 401

    def test_job_status_happy_path(self, client, auth_headers, enqueued):
        resp, pdf = _register(client, auth_headers)
        reg = resp.json()
        _redeem(client, auth_headers, reg["document_id"],
                reg["upload_ticket"], pdf)
        resp = client.get(f"/v1/jobs/{reg['job_id']}", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["job_id"] == reg["job_id"]
        assert body["job_type"] == "ingestion"
        assert body["state"] == "UPLOADED"
        assert body["document_id"] == reg["document_id"]
        assert body["document_version_id"] is not None
        assert body["attempt_count"] == 0
        assert body["error"] is None
        assert body["created_at"]
        assert body["updated_at"]

    def test_job_status_unknown_is_404(self, client, auth_headers):
        resp = client.get(f"/v1/jobs/{uuid.uuid4()}", headers=auth_headers)
        assert resp.status_code == 404

    def test_job_status_other_tenant_is_404(self, client, settings,
                                            auth_headers):
        headers_b = signed_headers(settings.internal_secret, group_id="tenant-b")
        reg = _register(client, headers_b)[0].json()
        resp = client.get(f"/v1/jobs/{reg['job_id']}", headers=auth_headers)
        assert resp.status_code == 404

    def test_job_status_reports_error_excerpt(self, client, auth_headers,
                                              session_factory):
        # Fail the reserved job directly through the repository, then check
        # the API surfaces a truncated excerpt (500 chars + "…").
        from services.catalog.models import IngestionState
        from services.catalog.repository import CatalogRepository
        from services.common.request_context import RequestContext

        resp, _ = _register(client, auth_headers)
        job_id = resp.json()["job_id"]
        session = session_factory()
        try:
            repo = CatalogRepository(
                session,
                RequestContext(user_id="user-1", group_id="tenant-a"),
            )
            repo.update_job_state(
                uuid.UUID(job_id), IngestionState.FAILED, error="x" * 600
            )
            session.commit()
        finally:
            session.close()
        resp = client.get(f"/v1/jobs/{job_id}", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        error = resp.json()["error"]
        assert error == "x" * 500 + "…"


# ---------------------------------------------------------------------------
# 403 where roles apply (existing review RBAC, unchanged by this change)
# ---------------------------------------------------------------------------


class TestRoleGatingUnchanged:
    def test_review_resolve_still_requires_reviewer(
        self, client, auth_headers, settings
    ):
        # auth_headers carries no roles → 403 on the reviewer-gated route.
        resp = client.post(
            f"/v1/review/items/{uuid.uuid4()}/resolve",
            headers=auth_headers,
            json={"decision": "accepted", "detail": {}},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Commit-before-enqueue recovery regressions
# ---------------------------------------------------------------------------


class TestCommitBeforeEnqueueRecovery:
    def test_redeem_recovers_after_enqueue_failure_and_version_marked_failed(
        self, client, auth_headers, enqueued, monkeypatch, session_factory
    ):
        import routes.documents as documents_routes
        from services.catalog.models import DocumentVersion, IngestionState

        resp, pdf = _register(client, auth_headers)
        reg = resp.json()

        # Simulate transient Redis/RQ outage during first redeem.
        def broken_enqueue(*a, **kw):
            raise RuntimeError("redis connection refused")

        monkeypatch.setattr(documents_routes, "enqueue_rq", broken_enqueue)
        failed_resp = _redeem(
            client, auth_headers, reg["document_id"], reg["upload_ticket"], pdf
        )
        assert failed_resp.status_code == 500

        # Both Job and DocumentVersion must be marked FAILED (not stranded in UPLOADED).
        sess = session_factory()
        try:
            ver = (
                sess.query(DocumentVersion)
                .filter_by(document_id=uuid.UUID(reg["document_id"]))
                .one()
            )
            assert ver.status == IngestionState.FAILED
        finally:
            sess.close()

        # Restore working queue and retry redemption with the same ticket.
        def working_enqueue(queue_name, entrypoint, job, redis_url):
            enqueued.append(
                {
                    "queue_name": queue_name,
                    "entrypoint": entrypoint,
                    "job_id": str(job.id),
                }
            )

        monkeypatch.setattr(documents_routes, "enqueue_rq", working_enqueue)
        recovered = _redeem(
            client, auth_headers, reg["document_id"], reg["upload_ticket"], pdf
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["status"] == "UPLOADED"
        assert len(enqueued) == 1

    def test_delete_recovers_after_enqueue_failure(
        self, client, auth_headers, enqueued, monkeypatch
    ):
        import routes.documents as documents_routes

        resp, pdf = _register(client, auth_headers)
        reg = resp.json()
        _redeem(client, auth_headers, reg["document_id"], reg["upload_ticket"], pdf)
        enqueued.clear()

        def broken_enqueue(*a, **kw):
            raise RuntimeError("redis down")

        monkeypatch.setattr(documents_routes, "enqueue_rq", broken_enqueue)
        first = client.delete(f"/v1/documents/{reg['document_id']}", headers=auth_headers)
        assert first.status_code == 500

        def working_enqueue(queue_name, entrypoint, job, redis_url):
            enqueued.append(
                {
                    "queue_name": queue_name,
                    "entrypoint": entrypoint,
                    "job_id": str(job.id),
                }
            )

        monkeypatch.setattr(documents_routes, "enqueue_rq", working_enqueue)
        second = client.delete(f"/v1/documents/{reg['document_id']}", headers=auth_headers)
        assert second.status_code == 202, second.text
        assert second.json()["status"] == "UPLOADED"
        assert len(enqueued) == 1

    def test_job_retry_endpoint_reenqueues_failed_upload_job(
        self, client, auth_headers, enqueued, monkeypatch
    ):
        import routes.documents as documents_routes

        pdf = make_pdf(1)

        def broken_enqueue(*a, **kw):
            raise RuntimeError("redis timeout")

        monkeypatch.setattr(documents_routes, "enqueue_rq", broken_enqueue)
        up = client.post(
            "/v1/documents/upload",
            headers=auth_headers,
            files={"file": ("doc.pdf", io.BytesIO(pdf), "application/pdf")},
        )
        assert up.status_code == 500

        docs = client.get("/v1/documents", headers=auth_headers).json()["documents"]
        doc_id = docs[0]["document_id"]
        st = client.get(f"/v1/documents/{doc_id}/status", headers=auth_headers).json()
        assert st["state"] == "FAILED"
        job_id = st["job_id"]

        def working_enqueue(queue_name, entrypoint, job, redis_url):
            enqueued.append({"queue_name": queue_name, "job_id": str(job.id)})

        monkeypatch.setattr(documents_routes, "enqueue_rq", working_enqueue)
        retried = client.post(f"/v1/jobs/{job_id}/retry", headers=auth_headers)
        assert retried.status_code == 200, retried.text
        assert retried.json()["state"] == "UPLOADED"
        assert len(enqueued) == 1
