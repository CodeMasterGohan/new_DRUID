"""Admin route tests: cross-tenant operator views (spec §16).

- admin role can list documents/jobs/quotas across tenants
- non-admin → 403, unauthenticated → 401
- tenant filter narrows to one tenant
- quotas read the gateway's Redis counter scheme (faked here)
"""

from __future__ import annotations

import datetime as dt
import io

import pytest
from fastapi.testclient import TestClient

from deps import get_redis_client
from routes.admin import _quota_keys

from .helpers import make_pdf, signed_headers


def _admin_headers(settings, **kwargs):
    kwargs.setdefault("roles", ("admin",))
    return signed_headers(settings.internal_secret, **kwargs)


def _upload(client, headers, data: bytes, filename: str):
    return client.post(
        "/v1/documents/upload",
        headers=headers,
        files={"file": (filename, io.BytesIO(data), "application/pdf")},
    )


@pytest.fixture()
def two_tenant_docs(client, settings, enqueued):
    """One uploaded document (+ingestion job) in each of tenant-a/b."""
    for group in ("tenant-a", "tenant-b"):
        headers = signed_headers(settings.internal_secret, group_id=group)
        resp = _upload(client, headers, make_pdf(1), f"doc-{group}.pdf")
        assert resp.status_code == 201, resp.text
    return client


class FakeRedis:
    """Minimal stand-in for the gateway's quota counters."""

    def __init__(self, values: dict):
        self._values = values

    def get(self, key):
        return self._values.get(key)

    def zcard(self, key):
        return self._values.get(key, 0)


class BrokenRedis:
    def get(self, key):
        raise ConnectionError("redis down")

    def zcard(self, key):
        raise ConnectionError("redis down")


# ---------------------------------------------------------------------------
# GET /v1/admin/documents
# ---------------------------------------------------------------------------


class TestAdminDocuments:
    def test_lists_across_tenants(self, two_tenant_docs, settings):
        resp = two_tenant_docs.get(
            "/v1/admin/documents", headers=_admin_headers(settings)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["limit"] == 100 and body["offset"] == 0
        groups = {d["group_id"] for d in body["documents"]}
        assert groups == {"tenant-a", "tenant-b"}
        for d in body["documents"]:
            assert d["document_id"] and d["display_name"]
            assert d["status"] == "UPLOADED"  # fresh uploads
            assert d["updated_at"]

    def test_tenant_filter(self, two_tenant_docs, settings):
        resp = two_tenant_docs.get(
            "/v1/admin/documents?tenant=tenant-a",
            headers=_admin_headers(settings),
        )
        assert resp.status_code == 200, resp.text
        docs = resp.json()["documents"]
        assert docs and all(d["group_id"] == "tenant-a" for d in docs)

    def test_status_filter(self, two_tenant_docs, settings):
        headers = _admin_headers(settings)
        resp = two_tenant_docs.get(
            "/v1/admin/documents?status=UPLOADED", headers=headers
        )
        assert resp.status_code == 200
        assert len(resp.json()["documents"]) == 2
        resp = two_tenant_docs.get(
            "/v1/admin/documents?status=READY", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["documents"] == []

    def test_status_filter_invalid(self, two_tenant_docs, settings):
        resp = two_tenant_docs.get(
            "/v1/admin/documents?status=BOGUS",
            headers=_admin_headers(settings),
        )
        assert resp.status_code == 400

    def test_non_admin_forbidden(self, two_tenant_docs, settings):
        headers = signed_headers(settings.internal_secret)  # no roles
        resp = two_tenant_docs.get("/v1/admin/documents", headers=headers)
        assert resp.status_code == 403

    def test_reviewer_forbidden(self, two_tenant_docs, settings):
        headers = signed_headers(settings.internal_secret, roles=("reviewer",))
        resp = two_tenant_docs.get("/v1/admin/documents", headers=headers)
        assert resp.status_code == 403

    def test_unauthenticated_unauthorized(self, two_tenant_docs):
        resp = two_tenant_docs.get("/v1/admin/documents")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /v1/admin/jobs
# ---------------------------------------------------------------------------


class TestAdminJobs:
    def test_lists_across_tenants(self, two_tenant_docs, settings):
        resp = two_tenant_docs.get(
            "/v1/admin/jobs", headers=_admin_headers(settings)
        )
        assert resp.status_code == 200, resp.text
        jobs = resp.json()["jobs"]
        groups = {j["group_id"] for j in jobs}
        assert groups == {"tenant-a", "tenant-b"}
        for j in jobs:
            assert j["job_id"] and j["job_type"] == "ingestion"
            assert j["state"] == "UPLOADED"
            assert j["document_id"]  # task: tenant, document surfaced
            assert j["attempt_count"] == 0
            assert j["created_at"] and j["updated_at"]
            assert j["duration_seconds"] is not None and j["duration_seconds"] >= 0

    def test_tenant_filter(self, two_tenant_docs, settings):
        resp = two_tenant_docs.get(
            "/v1/admin/jobs?tenant=tenant-b", headers=_admin_headers(settings)
        )
        assert resp.status_code == 200, resp.text
        jobs = resp.json()["jobs"]
        assert jobs and all(j["group_id"] == "tenant-b" for j in jobs)

    def test_non_admin_forbidden(self, two_tenant_docs, settings):
        headers = signed_headers(settings.internal_secret)
        resp = two_tenant_docs.get("/v1/admin/jobs", headers=headers)
        assert resp.status_code == 403

    def test_unauthenticated_unauthorized(self, two_tenant_docs):
        resp = two_tenant_docs.get("/v1/admin/jobs")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /v1/admin/quotas
# ---------------------------------------------------------------------------


class TestAdminQuotas:
    def _quota_client(self, app, values):
        app.dependency_overrides[get_redis_client] = lambda: FakeRedis(values)
        return TestClient(app)

    def test_reports_usage_vs_limits(self, app, settings, session_factory):
        import uuid
        from sqlalchemy import select
        from services.catalog.models import IngestionAdmission, Tenant
        with session_factory() as session:
            tenant = session.scalar(select(Tenant).where(Tenant.name == "tenant-a"))
            session.add(IngestionAdmission(version_id=uuid.uuid4(), tenant_id=tenant.id,
                quota_day=dt.datetime.now(dt.timezone.utc).date(), pages=37))
            session.commit()
        today = dt.date.today()
        ingest_key, query_key = _quota_keys("tenant-a", today)
        values = {ingest_key: "37", query_key: 4}
        client = self._quota_client(app, values)
        with client:
            resp = client.get(
                "/v1/admin/quotas", headers=_admin_headers(settings)
            )
        assert resp.status_code == 200, resp.text
        tenants = {t["group_id"]: t for t in resp.json()["tenants"]}
        assert set(tenants) == {"tenant-a", "tenant-b"}
        ta = tenants["tenant-a"]
        assert ta["ingest_pages_used"] == 37
        assert ta["ingest_pages_limit"] == 1000
        assert ta["query_qps"] == 4
        assert ta["query_qps_limit"] == 10
        # No counters yet for tenant-b → zero usage, not null.
        tb = tenants["tenant-b"]
        assert tb["ingest_pages_used"] == 0
        assert tb["query_qps"] == 0

    def test_redis_down_reports_null(self, app, settings):
        app.dependency_overrides[get_redis_client] = lambda: BrokenRedis()
        with TestClient(app) as client:
            resp = client.get(
                "/v1/admin/quotas", headers=_admin_headers(settings)
            )
        assert resp.status_code == 200, resp.text
        for t in resp.json()["tenants"]:
            assert t["ingest_pages_used"] == 0  # SQL accounting remains available
            assert t["query_qps"] is None
            # Limits still reported — they come from settings, not Redis.
            assert t["ingest_pages_limit"] == 1000
            assert t["query_qps_limit"] == 10

    def test_non_admin_forbidden(self, app, settings):
        app.dependency_overrides[get_redis_client] = lambda: FakeRedis({})
        headers = signed_headers(settings.internal_secret)
        with TestClient(app) as client:
            resp = client.get("/v1/admin/quotas", headers=headers)
        assert resp.status_code == 403

    def test_unauthenticated_unauthorized(self, app, settings):
        app.dependency_overrides[get_redis_client] = lambda: FakeRedis({})
        with TestClient(app) as client:
            resp = client.get("/v1/admin/quotas")
        assert resp.status_code == 401
