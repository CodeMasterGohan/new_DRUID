"""web_ui tests — the critical surface is auth scoping (spec §4, §16).

Tenant isolation is proven against a mocked gateway that serves different
data per token: the tests assert the UI forwards the *caller's* token
unchanged and never mixes tenants, and that spoofed identity signals
(headers, params, form fields) are ignored.
"""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from .conftest import login_as, make_token


def _html(resp) -> str:
    assert resp.status_code == 200, resp.text
    return resp.text


# ---------------------------------------------------------------------------
# login / session
# ---------------------------------------------------------------------------


def test_login_invalid_token_rejected(client: TestClient):
    resp = client.post(
        "/login", data={"token": "garbage", "next": "/documents"}
    )
    assert resp.status_code == 401
    assert "druid_at" not in client.cookies


def test_login_wrong_secret_rejected(client: TestClient):
    token = make_token("group-a", secret="wrong-secret")
    resp = client.post("/login", data={"token": token, "next": "/documents"})
    assert resp.status_code == 401
    assert "druid_at" not in client.cookies


def test_login_expired_token_rejected(client: TestClient):
    token = make_token("group-a", expired=True)
    resp = client.post("/login", data={"token": token, "next": "/documents"})
    assert resp.status_code == 401


def test_login_sets_httponly_cookie(client: TestClient):
    token = make_token("group-a")
    resp = client.post(
        "/login", data={"token": token, "next": "/documents"}, follow_redirects=False
    )
    assert resp.status_code == 303
    set_cookie = resp.headers.get("set-cookie", "")
    assert "druid_at=" in set_cookie
    assert "httponly" in set_cookie.lower()


def test_login_roles_come_from_token_only(client: TestClient, mock_gateway):
    """A 'roles' form field must not grant anything — claims come from the JWT."""
    token = make_token("group-a", roles=("user",))
    resp = client.post(
        "/login",
        data={"token": token, "next": "/documents", "roles": "admin"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # The admin nav must not render: the session holds user-only roles.
    page = client.get("/documents")
    assert "Admin:" not in page.text


def test_unauthenticated_page_redirects_to_login(client: TestClient):
    resp = client.get("/documents", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_unauthenticated_fragment_returns_401(client: TestClient):
    resp = client.get("/partials/documents/rows")
    assert resp.status_code == 401


def test_logout_clears_cookie(client: TestClient):
    login_as(client, "group-a")
    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert "druid_at" not in client.cookies


# ---------------------------------------------------------------------------
# tenant isolation — the critical surface
# ---------------------------------------------------------------------------


def test_spoofed_tenant_headers_ignored(
    client: TestClient, mock_gateway
):
    """X-Group-Id / X-Druid-* headers must not influence identity."""
    token = login_as(client, "group-a")
    resp = client.get(
        "/documents",
        headers={
            "X-Group-Id": "group-b",
            "X-Druid-Group-Id": "group-b",
            "X-Druid-Roles": "admin",
        },
    )
    body = _html(resp)
    # The downstream call carried the cookie's token, untouched.
    assert mock_gateway.last_auth_header() == f"Bearer {token}"
    # ...and the page shows group-a's documents only.
    assert "report-group-a.pdf" in body
    assert "report-group-b.pdf" not in body


def test_spoofed_tenant_query_param_ignored(
    client: TestClient, mock_gateway
):
    login_as(client, "group-a")
    resp = client.get("/documents?group_id=group-b")
    body = _html(resp)
    assert "report-group-a.pdf" in body
    assert "report-group-b.pdf" not in body


def test_tenant_a_cannot_see_tenant_b_documents(client: TestClient):
    login_as(client, "group-a")
    body = _html(client.get("/documents"))
    assert "report-group-a.pdf" in body
    assert "report-group-b.pdf" not in body

    login_as(client, "group-b")
    body = _html(client.get("/documents"))
    assert "report-group-b.pdf" in body
    assert "report-group-a.pdf" not in body


def test_tenant_a_cannot_open_tenant_b_document(client: TestClient):
    """Direct detail fetch for another tenant's doc → downstream 404 → UI 404."""
    login_as(client, "group-a")
    resp = client.get("/documents/doc-group-b-1")
    assert resp.status_code == 404


def test_review_resolve_forwards_callers_token(client: TestClient, mock_gateway):
    token = login_as(client, "group-a", roles=("user", "reviewer"))
    resp = client.post(
        "/review/item-1/resolve",
        data={"decision": "accepted", "detail": "looks fine"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/review?notice=resolved"
    assert mock_gateway.last_auth_header() == f"Bearer {token}"
    assert mock_gateway.resolved[-1]["group"] == "group-a"
    assert mock_gateway.resolved[-1]["body"]["decision"] == "accepted"
    assert mock_gateway.resolved[-1]["body"]["detail"] == {"note": "looks fine"}


# ---------------------------------------------------------------------------
# roles: admin / reviewer gates
# ---------------------------------------------------------------------------


def test_admin_views_forbidden_for_user(client: TestClient):
    login_as(client, "group-a", roles=("user",))
    for path in ("/admin/documents", "/admin/jobs", "/admin/quotas"):
        resp = client.get(path)
        assert resp.status_code == 403, path


def test_admin_views_forbidden_for_reviewer(client: TestClient):
    login_as(client, "group-a", roles=("user", "reviewer"))
    resp = client.get("/admin/documents")
    assert resp.status_code == 403


def test_admin_views_allowed_for_admin(client: TestClient, mock_gateway):
    mock_gateway.admin_enabled = True
    login_as(client, "group-a", roles=("user", "admin"))
    for path in ("/admin/documents", "/admin/jobs", "/admin/quotas"):
        resp = client.get(path)
        assert resp.status_code == 200, path
    assert "group-a" in client.get("/admin/quotas").text


def test_admin_backend_missing_degrades_gracefully(client: TestClient):
    """Proposed /v1/admin/* contract absent downstream → explanatory page, 200."""
    login_as(client, "group-a", roles=("user", "admin"))
    for path in ("/admin/documents", "/admin/jobs", "/admin/quotas"):
        body = _html(client.get(path))
        assert "not" in body and "implemented by document_api yet" in body


def test_admin_nav_hidden_from_non_admin(client: TestClient):
    login_as(client, "group-a", roles=("user",))
    body = _html(client.get("/documents"))
    assert "Admin:" not in body
    assert "/admin/documents" not in body


def test_review_resolve_requires_reviewer_role(client: TestClient):
    login_as(client, "group-a", roles=("user",))
    resp = client.post(
        "/review/item-1/resolve", data={"decision": "accepted"}
    )
    assert resp.status_code == 403


def test_review_detail_hides_actions_without_reviewer(client: TestClient):
    login_as(client, "group-a", roles=("user",))
    body = _html(client.get("/review/item-1"))
    assert "requires the reviewer role" in body
    assert 'action="/review/item-1/resolve"' not in body


def test_review_detail_shows_actions_for_reviewer(client: TestClient):
    login_as(client, "group-a", roles=("user", "reviewer"))
    body = _html(client.get("/review/item-1"))
    assert 'action="/review/item-1/resolve"' in body


def test_review_detail_shows_actions_for_admin(client: TestClient):
    login_as(client, "group-a", roles=("user", "admin"))
    body = _html(client.get("/review/item-1"))
    assert 'action="/review/item-1/resolve"' in body


# ---------------------------------------------------------------------------
# upload validation + chunked protocol
# ---------------------------------------------------------------------------


def _start_session(client: TestClient, filename: str, total_chunks: int, total_bytes: int):
    resp = client.post(
        "/upload/session",
        data={
            "filename": filename,
            "content_type": "application/pdf",
            "total_chunks": str(total_chunks),
            "total_bytes": str(total_bytes),
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def test_upload_rejects_empty_file(client: TestClient):
    login_as(client, "group-a")
    resp = client.post(
        "/upload/session",
        data={
            "filename": "empty.pdf",
            "content_type": "application/pdf",
            "total_chunks": "1",
            "total_bytes": "0",
        },
    )
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"]


def test_upload_rejects_bad_extension(client: TestClient):
    login_as(client, "group-a")
    resp = client.post(
        "/upload/session",
        data={
            "filename": "evil.exe",
            "content_type": "application/octet-stream",
            "total_chunks": "1",
            "total_bytes": "100",
        },
    )
    assert resp.status_code == 400
    assert "unsupported file type" in resp.json()["detail"]


def test_upload_rejects_oversize(client: TestClient, ui_settings):
    login_as(client, "group-a")
    limit = ui_settings.max_upload_mb * 1024 * 1024
    resp = client.post(
        "/upload/session",
        data={
            "filename": "big.pdf",
            "content_type": "application/pdf",
            "total_chunks": "1",
            "total_bytes": str(limit + 1),
        },
    )
    assert resp.status_code == 400
    assert "exceeds" in resp.json()["detail"]


def test_chunked_upload_roundtrip_with_resume(client: TestClient, mock_gateway):
    """Out-of-order chunk delivery + resume state, then assembly integrity."""
    login_as(client, "group-a")
    payload = b"%PDF-1.7 fake content " * 5000  # ~100KB
    half = len(payload) // 2
    chunks = [payload[:half], payload[half:]]

    sid = _start_session(client, "doc.pdf", 2, len(payload))

    # Deliver chunk 1 first (out of order); state must show only [1].
    resp = client.post(
        f"/upload/session/{sid}/chunk?index=1",
        files={"chunk": ("c1", chunks[1])},
    )
    assert resp.status_code == 200
    assert resp.json()["received"] == [1]

    state = client.get(f"/upload/session/{sid}")
    assert state.json() == {"received": [1], "total": 2}

    # Complete too early → 400.
    assert client.post(f"/upload/session/{sid}/complete").status_code == 400

    # Deliver chunk 0, then complete.
    resp = client.post(
        f"/upload/session/{sid}/chunk?index=0",
        files={"chunk": ("c0", chunks[0])},
    )
    assert resp.json()["received"] == [0, 1]
    done = client.post(f"/upload/session/{sid}/complete")
    assert done.status_code == 201, done.text
    assert done.json()["document_id"] == "doc-new-1"

    # The gateway received ONE assembled multipart upload with intact bytes.
    upload_req = mock_gateway.requests[-1]
    assert upload_req.url.path == "/v1/documents/upload"
    assert upload_req.headers["authorization"].startswith("Bearer ")
    assert payload in upload_req.content


def test_chunk_index_out_of_range_rejected(client: TestClient):
    login_as(client, "group-a")
    sid = _start_session(client, "doc.pdf", 2, 100)
    resp = client.post(
        f"/upload/session/{sid}/chunk?index=7",
        files={"chunk": ("c", b"x")},
    )
    assert resp.status_code == 400


def test_unknown_session_404(client: TestClient):
    login_as(client, "group-a")
    assert client.get("/upload/session/nope").status_code == 404
    assert (
        client.post("/upload/session/nope/complete").status_code == 404
    )


# ---------------------------------------------------------------------------
# search provenance
# ---------------------------------------------------------------------------


def test_search_renders_filename_and_page_range(client: TestClient):
    login_as(client, "group-a")
    resp = client.post(
        "/partials/search", data={"q": "what is the fox?", "top_k": "8"}
    )
    body = _html(resp)
    assert "report-group-a.pdf" in body
    assert 'href="/documents/doc-group-a-1"' in body
    assert "3" in body and "5" in body  # page_start–page_end
    assert "The quick brown fox." in body


def test_search_empty_query_prompt(client: TestClient):
    login_as(client, "group-a")
    resp = client.post("/partials/search", data={"q": "   ", "top_k": "8"})
    assert resp.status_code == 200
    assert "Enter a question" in resp.text


# ---------------------------------------------------------------------------
# misc pages
# ---------------------------------------------------------------------------


def test_documents_page_lists_with_status(client: TestClient):
    login_as(client, "group-a")
    body = _html(client.get("/documents"))
    assert "report-group-a.pdf" in body
    assert "READY" in body


def test_document_detail_shows_versions(client: TestClient):
    login_as(client, "group-a")
    body = _html(client.get("/documents/doc-group-a-1"))
    assert "report-group-a.pdf" in body
    assert "active" in body
    assert "12" in body  # page count


def test_quarantine_page_renders(client: TestClient):
    login_as(client, "group-a")
    assert _html(client.get("/quarantine"))  # 200, no FAILED docs in mock


def test_document_delete_redirects_with_notice(client: TestClient):
    login_as(client, "group-a")
    resp = client.post("/documents/doc-group-a-1/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/documents?notice=deleted"


def test_active_nav_tab_highlighted(client: TestClient):
    login_as(client, "group-a")
    body = _html(client.get("/documents"))
    assert '<a href="/documents" class="active">Documents</a>' in body
    assert '<a href="/upload" class="">Upload</a>' in body


def test_notice_banner_renders(client: TestClient):
    login_as(client, "group-a")
    body = _html(client.get("/documents?notice=deleted"))
    assert 'class="notice"' in body
    assert "Deletion queued" in body


def test_login_redirect_is_same_origin_only(client: TestClient):
    token = make_token("group-a")
    for unsafe in (
        "//external.invalid/path",
        "///external.invalid",
        "/%2fexternal.invalid",
        "/%255cexternal.invalid",
        "/\\external.invalid",
        "https://external.invalid/",
    ):
        response = client.post(
            "/login", data={"token": token, "next": unsafe}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/documents"
    safe = client.post(
        "/login", data={"token": token, "next": "/documents?sort=recent"},
        follow_redirects=False,
    )
    assert safe.status_code == 303
    assert safe.headers["location"] == "/documents?sort=recent"


def test_upload_session_isolated_per_user_and_tenant(client: TestClient):
    login_as(client, "group-a")
    sid = _start_session(client, "doc.pdf", 1, 10)
    # Different tenant cannot view, upload chunk, or complete group-a's session.
    login_as(client, "group-b")
    assert client.get(f"/upload/session/{sid}").status_code == 404
    assert (
        client.post(
            f"/upload/session/{sid}/chunk?index=0",
            files={"chunk": ("c0", b"0123456789")},
        ).status_code
        == 404
    )
    assert client.post(f"/upload/session/{sid}/complete").status_code == 404


def test_upload_session_enforces_cumulative_max_bytes():
    from upload_sessions import UploadSessionStore
    import pytest

    store = UploadSessionStore()
    sess = store.create("doc.pdf", "application/pdf", 2, max_bytes=10)
    store.add_chunk(sess.session_id, 0, b"123456")
    with pytest.raises(ValueError, match="exceed maximum upload size"):
        store.add_chunk(sess.session_id, 1, b"12345")
