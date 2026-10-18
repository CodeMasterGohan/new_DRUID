"""Operator traces must remain opt-in and tenant-authorized."""
import hashlib
import hmac
import time

from fastapi.testclient import TestClient

from services.retrieval import main as api
from services.retrieval.tests.test_retrieval import (
    FakeStore, make_context, make_record, make_service,
)


def _headers(role="reader"):
    timestamp = str(time.time())
    payload = f"u1|tenant-a|{role}|req1|{timestamp}"
    signature = hmac.new(b"test-internal", payload.encode(), hashlib.sha256).hexdigest()
    return {
        "x-druid-user": "u1", "x-druid-group": "tenant-a",
        "x-druid-roles": role, "x-druid-request-id": "req1",
        "x-druid-timestamp": timestamp, "x-druid-signature": signature,
    }


def test_trace_records_branch_counts_and_no_document_text():
    store = FakeStore(dense=[make_record("c1", text="classified sample")])
    svc = make_service(store=store)
    trace = {}
    results = svc.search(make_context(), "question", diagnostics=trace)
    assert len(results) == 1
    assert trace["authorized_versions"] == 1
    assert trace["branches"]["dense"] == 1
    assert trace["result_count"] == 1
    assert "total" in trace["timings_ms"]
    assert "classified sample" not in str(trace)


def test_diagnostics_require_both_admin_and_explicit_server_opt_in(monkeypatch):
    monkeypatch.setenv("INTERNAL_SECRET", "test-internal")
    monkeypatch.setattr(api, "_service", make_service(
        store=FakeStore(dense=[make_record("c1")])
    ))
    with TestClient(api.app) as client:
        default = client.post("/search", headers=_headers("reader"),
                              json={"query": "question"})
        assert default.status_code == 200
        assert "diagnostics" not in default.json()

        denied = client.post("/search", headers=_headers("reader"),
                             json={"query": "question", "diagnostics": True})
        assert denied.status_code == 403

        monkeypatch.setenv("DRUID_RETRIEVAL_DIAGNOSTICS", "1")
        denied_reader = client.post("/search", headers=_headers("reader"),
                                    json={"query": "question", "diagnostics": True})
        assert denied_reader.status_code == 403

        granted = client.post("/search", headers=_headers("admin"),
                              json={"query": "question", "diagnostics": True})
        assert granted.status_code == 200
        assert granted.json()["diagnostics"]["result_count"] == 1


def test_unsigned_diagnostics_still_fail_closed(monkeypatch):
    monkeypatch.setenv("INTERNAL_SECRET", "test-internal")
    monkeypatch.setenv("DRUID_RETRIEVAL_DIAGNOSTICS", "1")
    monkeypatch.setattr(api, "_service", make_service())
    with TestClient(api.app) as client:
        response = client.post("/search", json={"query": "hello", "diagnostics": True})
        assert response.status_code == 401
