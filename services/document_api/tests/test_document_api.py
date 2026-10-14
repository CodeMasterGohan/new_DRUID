"""document_api tests: auth, upload validation, tenant scoping, deletion, review RBAC."""

from __future__ import annotations

import io
import uuid

import pytest

from .helpers import make_pdf, make_png, signed_headers


# ---------------------------------------------------------------------------
# internal auth (HMAC X-Druid-* verification)
# ---------------------------------------------------------------------------


class TestInternalAuth:
    def test_missing_headers_is_401(self, client):
        resp = client.get("/v1/documents")
        assert resp.status_code == 401

    def test_bad_signature_is_401(self, client, settings):
        headers = signed_headers(settings.internal_secret)
        headers["X-Druid-Internal-Sig"] = "v1:" + "0" * 64
        resp = client.get("/v1/documents", headers=headers)
        assert resp.status_code == 401

    def test_wrong_secret_is_401(self, client):
        headers = signed_headers("some-other-secret")
        resp = client.get("/v1/documents", headers=headers)
        assert resp.status_code == 401

    def test_tampered_group_id_is_401(self, client, settings):
        headers = signed_headers(settings.internal_secret, group_id="tenant-a")
        headers["X-Druid-Group-Id"] = "tenant-b"  # signed for tenant-a
        resp = client.get("/v1/documents", headers=headers)
        assert resp.status_code == 401

    def test_tampered_roles_is_401(self, client, settings):
        headers = signed_headers(settings.internal_secret, roles=())
        headers["X-Druid-Roles"] = "reviewer"  # not in the signature
        resp = client.get("/v1/documents", headers=headers)
        assert resp.status_code == 401

    def test_valid_headers_ok(self, client, auth_headers):
        resp = client.get("/v1/documents", headers=auth_headers)
        assert resp.status_code == 200

    def test_unconfigured_secret_fails_closed(self, settings, session_factory,
                                              object_store):
        """No INTERNAL_SECRET → every request 401, never anonymous."""
        from main import create_app

        bad = settings.model_copy(update={"internal_secret": ""})
        app = create_app(settings=bad, session_factory=session_factory,
                         object_store=object_store)
        from fastapi.testclient import TestClient

        with TestClient(app) as c:
            resp = c.get("/v1/documents",
                         headers=signed_headers("test-internal-secret"))
            assert resp.status_code == 401


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


def _upload(client, headers, data: bytes, filename: str,
            content_type: str = "application/pdf"):
    return client.post(
        "/v1/documents/upload",
        headers=headers,
        files={"file": (filename, io.BytesIO(data), content_type)},
    )


class TestUpload:
    def test_upload_pdf_happy_path(self, client, auth_headers, enqueued,
                                   object_store):
        pdf = make_pdf(2)
        resp = _upload(client, auth_headers, pdf, "report.pdf")
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["page_count"] == 2
        assert body["status"] == "UPLOADED"
        assert uuid.UUID(body["document_id"])
        assert uuid.UUID(body["job_id"])
        # Job enqueued on the ingest queue with the pipeline entry point.
        assert len(enqueued) == 1
        assert enqueued[0]["queue_name"] == "ingest"
        assert enqueued[0]["entrypoint"].endswith("run_ingestion_job")
        assert enqueued[0]["job_id"] == body["job_id"]
        # Original stored under a tenant-scoped key (filename never in key).
        keys = list(object_store._data.keys())
        assert len(keys) == 1
        assert keys[0].startswith("tenant-a/")
        assert "report.pdf" not in keys[0]
        assert object_store._data[keys[0]] == pdf

    def test_upload_png(self, client, auth_headers, enqueued):
        resp = _upload(client, auth_headers, make_png(), "scan.png",
                       content_type="image/png")
        assert resp.status_code == 201, resp.text
        assert resp.json()["page_count"] == 1

    def test_upload_rejects_garbage(self, client, auth_headers):
        resp = _upload(client, auth_headers, b"not a document at all", "x.pdf")
        assert resp.status_code == 400

    def test_upload_rejects_mime_mismatch(self, client, auth_headers):
        # Declared PNG but magic bytes say PDF.
        resp = _upload(client, auth_headers, make_pdf(1), "x.png",
                       content_type="image/png")
        assert resp.status_code == 400

    def test_upload_rejects_empty(self, client, auth_headers):
        resp = _upload(client, auth_headers, b"", "empty.pdf")
        assert resp.status_code == 400

    def test_upload_enforces_size_cap(self, client, auth_headers, settings):
        settings.max_upload_mb = 0  # any byte exceeds the cap
        resp = _upload(client, auth_headers, make_pdf(1), "big.pdf")
        assert resp.status_code == 413

    def test_upload_enforces_page_limit(self, client, auth_headers):
        # settings cap is 3 pages; upload 5.
        resp = _upload(client, auth_headers, make_pdf(5), "long.pdf")
        assert resp.status_code == 400
        assert "pages" in resp.json()["detail"]

    def test_upload_requires_auth(self, client, enqueued):
        resp = _upload(client, {}, make_pdf(1), "x.pdf")
        assert resp.status_code == 401
        assert enqueued == []

    def test_filename_is_display_only(self, client, auth_headers, enqueued):
        resp = _upload(client, auth_headers, make_pdf(1), "../../etc/passwd")
        assert resp.status_code == 201, resp.text
        assert resp.json()["display_name"] == "passwd"


# ---------------------------------------------------------------------------
# tenant scoping
# ---------------------------------------------------------------------------


class TestTenantScoping:
    def _upload_as(self, client, settings, group_id):
        headers = signed_headers(settings.internal_secret, group_id=group_id)
        resp = _upload(client, headers, make_pdf(1), "doc.pdf")
        assert resp.status_code == 201, resp.text
        return resp.json()["document_id"]

    def test_list_is_tenant_scoped(self, client, settings, auth_headers, enqueued):
        doc_a = self._upload_as(client, settings, "tenant-a")
        doc_b = self._upload_as(client, settings, "tenant-b")
        resp = client.get("/v1/documents", headers=auth_headers)
        ids = {d["document_id"] for d in resp.json()["documents"]}
        assert doc_a in ids and doc_b not in ids

    def test_get_other_tenant_document_is_404(self, client, settings, auth_headers, enqueued):
        doc_b = self._upload_as(client, settings, "tenant-b")
        resp = client.get(f"/v1/documents/{doc_b}", headers=auth_headers)
        assert resp.status_code == 404

    def test_get_document_detail(self, client, auth_headers, enqueued):
        resp = _upload(client, auth_headers, make_pdf(1), "detail.pdf")
        doc_id = resp.json()["document_id"]
        resp = client.get(f"/v1/documents/{doc_id}", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["document_id"] == doc_id
        assert len(body["versions"]) == 1
        assert body["versions"][0]["page_count"] == 1
        assert "original" in body["artifacts"]
        assert body["artifacts"]["original"]["key"].startswith("tenant-a/")

    def test_status_reports_ingestion_job(self, client, auth_headers, enqueued):
        resp = _upload(client, auth_headers, make_pdf(1), "s.pdf")
        doc_id, job_id = resp.json()["document_id"], resp.json()["job_id"]
        resp = client.get(f"/v1/documents/{doc_id}/status", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == job_id
        assert body["job_type"] == "ingestion"
        assert body["state"] == "UPLOADED"

    def test_status_other_tenant_is_404(self, client, settings, auth_headers, enqueued):
        doc_b = self._upload_as(client, settings, "tenant-b")
        resp = client.get(f"/v1/documents/{doc_b}/status", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# deletion
# ---------------------------------------------------------------------------


class TestDeletion:
    def test_delete_enqueues_deletion_job(self, client, auth_headers, enqueued):
        doc_id = _upload(client, auth_headers, make_pdf(1), "d.pdf").json()["document_id"]
        enqueued.clear()
        resp = client.delete(f"/v1/documents/{doc_id}", headers=auth_headers)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["document_id"] == doc_id
        assert len(enqueued) == 1
        assert enqueued[0]["queue_name"] == "delete"
        assert enqueued[0]["entrypoint"].endswith("run_deletion_job")

    def test_delete_is_idempotent(self, client, auth_headers, enqueued):
        doc_id = _upload(client, auth_headers, make_pdf(1), "d.pdf").json()["document_id"]
        first = client.delete(f"/v1/documents/{doc_id}", headers=auth_headers)
        assert first.status_code == 202
        second = client.delete(f"/v1/documents/{doc_id}", headers=auth_headers)
        assert second.status_code == 202
        # Same job reported; only one enqueue happened.
        assert second.json()["job_id"] == first.json()["job_id"]
        assert len([c for c in enqueued if c["queue_name"] == "delete"]) == 1

    def test_delete_other_tenant_is_404(self, client, settings, auth_headers, enqueued):
        headers_b = signed_headers(settings.internal_secret, group_id="tenant-b")
        doc_b = _upload(client, headers_b, make_pdf(1), "d.pdf").json()["document_id"]
        n_before = len(enqueued)
        resp = client.delete(f"/v1/documents/{doc_b}", headers=auth_headers)
        assert resp.status_code == 404
        assert len(enqueued) == n_before

    def test_delete_missing_is_404(self, client, auth_headers):
        resp = client.delete(f"/v1/documents/{uuid.uuid4()}", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# review queue
# ---------------------------------------------------------------------------


def _seed_quarantine_item(session_factory, object_store):
    """Park one page in the review queue for tenant-a, returning its id."""
    import uuid as _uuid

    from services.catalog.models import Document, DocumentVersion, Job, Tenant, IngestionState
    from services.catalog.repository import CatalogRepository
    from services.common.request_context import RequestContext

    session = session_factory()
    tenant = session.query(Tenant).filter_by(name="tenant-a").one()
    ctx = RequestContext(user_id="worker", group_id="tenant-a", roles=())
    repo = CatalogRepository(session, ctx)
    doc = repo.create_document(display_name="q.pdf", content_checksum="abc")
    version = repo.create_version(doc.id, parser="mixed", parser_revision="router-v1",
                                  page_count=1)
    item = repo.quarantine_page(
        document_id=doc.id,
        document_version_id=version.id,
        job_id=None,
        page_index=0,
        page_render_ref="tenant-a/render.png",
        parse_attempts={"native": {"text": "x"}, "visual": {"markdown": "y"}},
        routing_diagnostics={"coverage": 0.1},
    )
    session.commit()
    item_id = str(item.id)
    doc_id = str(doc.id)
    session.close()
    return item_id, doc_id


class TestReviewQueue:
    def test_list_review_items(self, client, auth_headers, session_factory,
                               object_store):
        item_id, _ = _seed_quarantine_item(session_factory, object_store)
        resp = client.get("/v1/review/items", headers=auth_headers)
        assert resp.status_code == 200
        ids = {i["item_id"] for i in resp.json()["items"]}
        assert item_id in ids

    def test_list_review_items_tenant_scoped(self, client, settings,
                                             session_factory, object_store):
        item_id, _ = _seed_quarantine_item(session_factory, object_store)
        headers_b = signed_headers(settings.internal_secret, group_id="tenant-b")
        resp = client.get("/v1/review/items", headers=headers_b)
        assert resp.status_code == 200
        ids = {i["item_id"] for i in resp.json()["items"]}
        assert item_id not in ids

    def test_get_review_item(self, client, auth_headers, session_factory,
                             object_store):
        item_id, _ = _seed_quarantine_item(session_factory, object_store)
        resp = client.get(f"/v1/review/items/{item_id}", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["parse_attempts"]["native"]["text"] == "x"
        assert body["routing_diagnostics"]["coverage"] == 0.1
        assert body["page_render_key"] == "tenant-a/render.png"

    def test_resolve_requires_reviewer_role(self, client, auth_headers,
                                            session_factory, object_store):
        item_id, _ = _seed_quarantine_item(session_factory, object_store)
        resp = client.post(
            f"/v1/review/items/{item_id}/resolve",
            headers=auth_headers,  # no roles
            json={"decision": "accepted"},
        )
        assert resp.status_code == 403

    def test_resolve_with_reviewer_role(self, client, settings, session_factory,
                                        object_store):
        item_id, _ = _seed_quarantine_item(session_factory, object_store)
        headers = signed_headers(settings.internal_secret, roles=("reviewer",))
        resp = client.post(
            f"/v1/review/items/{item_id}/resolve",
            headers=headers,
            json={"decision": "accepted", "detail": {"chosen": "visual"}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "accepted"
        # Second resolve conflicts.
        resp = client.post(
            f"/v1/review/items/{item_id}/resolve",
            headers=headers,
            json={"decision": "rejected"},
        )
        assert resp.status_code == 409

    def test_resolve_with_admin_role(self, client, settings, session_factory,
                                     object_store):
        item_id, _ = _seed_quarantine_item(session_factory, object_store)
        headers = signed_headers(settings.internal_secret, roles=("admin",))
        resp = client.post(
            f"/v1/review/items/{item_id}/resolve",
            headers=headers,
            json={"decision": "accepted", "detail": {"chosen": "admin_override"}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "accepted"

    def test_resolve_invalid_decision_rejected(self, client, settings,
                                              session_factory, object_store):
        item_id, _ = _seed_quarantine_item(session_factory, object_store)
        headers = signed_headers(settings.internal_secret, roles=("reviewer",))
        resp = client.post(
            f"/v1/review/items/{item_id}/resolve",
            headers=headers,
            json={"decision": "maybe"},
        )
        assert resp.status_code == 422

    def test_documents_review_required_filter(self, client, auth_headers,
                                             session_factory, object_store):
        _, doc_id = _seed_quarantine_item(session_factory, object_store)
        resp = client.get("/v1/documents?review_required=true", headers=auth_headers)
        ids = {d["document_id"] for d in resp.json()["documents"]}
        assert doc_id in ids
        resp = client.get("/v1/documents?review_required=false", headers=auth_headers)
        ids = {d["document_id"] for d in resp.json()["documents"]}
        assert doc_id not in ids
