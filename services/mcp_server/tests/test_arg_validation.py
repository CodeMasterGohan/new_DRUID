"""Argument validation tests (§11): opaque IDs, filenames, MIME allowlist,
checksums, bounds, and the confirm gate on mutating tools."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server.models import (
    DeleteDocumentArgs,
    GetDocumentArgs,
    IngestArgs,
    ListDocumentsArgs,
    ResolveReviewItemArgs,
    SearchArgs,
)

VALID_SHA = "a" * 64


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../etc/passwd",
        "/abs/path",
        "a/b",
        "a b",
        "",
        "x" * 129,
        "doc;DROP TABLE",
        "..",
    ],
)
def test_opaque_id_rejects_paths_and_junk(bad_id):
    with pytest.raises(ValidationError):
        GetDocumentArgs(document_id=bad_id)


@pytest.mark.parametrize("good_id", ["doc_1", "job-abc_123", "ABC", "x" * 128])
def test_opaque_id_accepts_sane_ids(good_id):
    assert GetDocumentArgs(document_id=good_id).document_id == good_id


@pytest.mark.parametrize(
    "bad_name",
    [
        "../secret.pdf",
        "/etc/passwd.pdf",
        "sub\\dir\\x.pdf",
        " notes.pdf",
        ".hidden.pdf",
        "x" * 252 + ".pdf",  # > 255 chars
        "report.docx",  # unsupported extension
        "report",  # no extension
    ],
)
def test_filename_rejects_paths_and_bad_types(bad_name):
    with pytest.raises(ValidationError):
        IngestArgs(
            filename=bad_name,
            mime_type="application/pdf",
            file_size_bytes=100,
            content_sha256=VALID_SHA,
        )


@pytest.mark.parametrize(
    "good_name", ["report.pdf", "scan 2024-01-02.PNG", "a.jpg", "img.tiff"]
)
def test_filename_accepts_bare_names(good_name):
    args = IngestArgs(
        filename=good_name,
        mime_type="application/pdf",
        file_size_bytes=100,
        content_sha256=VALID_SHA,
    )
    assert args.filename == good_name


def test_ingest_rejects_disallowed_mime():
    with pytest.raises(ValidationError):
        IngestArgs(
            filename="x.pdf",
            mime_type="application/zip",
            file_size_bytes=100,
            content_sha256=VALID_SHA,
        )


@pytest.mark.parametrize("bad_sha", ["abc", "z" * 64, "a" * 63, "a" * 65, ""])
def test_ingest_rejects_malformed_sha256(bad_sha):
    with pytest.raises(ValidationError):
        IngestArgs(
            filename="x.pdf",
            mime_type="application/pdf",
            file_size_bytes=100,
            content_sha256=bad_sha,
        )


def test_ingest_rejects_oversize_file():
    with pytest.raises(ValidationError):
        IngestArgs(
            filename="x.pdf",
            mime_type="application/pdf",
            file_size_bytes=200 * 1024 * 1024 + 1,
            content_sha256=VALID_SHA,
        )


def test_search_rejects_blank_or_huge_query():
    with pytest.raises(ValidationError):
        SearchArgs(query="   ")
    with pytest.raises(ValidationError):
        SearchArgs(query="x" * 2001)


@pytest.mark.parametrize("bad_k", [0, -1, 21, 10_000])
def test_search_rejects_out_of_range_top_k(bad_k):
    with pytest.raises(ValidationError):
        SearchArgs(query="hello", top_k=bad_k)


def test_list_documents_bounds():
    with pytest.raises(ValidationError):
        ListDocumentsArgs(limit=0)
    with pytest.raises(ValidationError):
        ListDocumentsArgs(limit=101)
    with pytest.raises(ValidationError):
        ListDocumentsArgs(offset=-1)
    with pytest.raises(ValidationError):
        ListDocumentsArgs(status="NOT_A_STATE")
    ok = ListDocumentsArgs(review_required=True, status="READY", limit=10, offset=5)
    assert ok.review_required is True


def test_resolve_decision_enum():
    with pytest.raises(ValidationError):
        ResolveReviewItemArgs(review_item_id="r1", decision="maybe", confirm=True)
    ok = ResolveReviewItemArgs(review_item_id="r1", decision="accept", confirm=True)
    assert ok.decision == "accept"


def test_delete_confirm_defaults_false():
    assert DeleteDocumentArgs(document_id="d1").confirm is False
