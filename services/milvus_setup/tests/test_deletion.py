"""Deletion-helper tests — no live Milvus required."""

import pytest

from ..deletion import DeleteResult, build_delete_expr, delete_document


def test_expr_includes_tenant_predicate():
    expr = build_delete_expr("tenant-1", "doc_abc123")
    assert 'group_id == "tenant-1"' in expr
    assert 'document_id == "doc_abc123"' in expr
    assert " and " in expr


def test_expr_rejects_injection_attempts():
    for bad in (
        't1" or "1"=="1',
        "t1'; DROP TABLE x; --",
        "tenant\n1",
        "",
        "x" * 129,
        "../etc/passwd",
        "tenant 1",
    ):
        with pytest.raises(ValueError):
            build_delete_expr(bad, "doc1")
        with pytest.raises(ValueError):
            build_delete_expr("tenant1", bad)


def test_expr_rejects_non_strings():
    with pytest.raises(ValueError):
        build_delete_expr(None, "doc1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_delete_expr("tenant1", 123)  # type: ignore[arg-type]


class FakeClient:
    def __init__(self, delete_count=3):
        self.calls = []
        self._count = delete_count

    def delete(self, collection_name, filter):
        self.calls.append((collection_name, filter))
        return {"delete_count": self._count}


def test_delete_document_uses_alias_and_tenant_scope():
    client = FakeClient()
    result = delete_document(client, "tenant-9", "doc-xyz")
    assert isinstance(result, DeleteResult)
    assert result.delete_count == 3
    (collection_name, expr), = client.calls
    assert collection_name == "project_knowledge_base"  # the alias, not v1
    assert 'group_id == "tenant-9"' in expr
    assert 'document_id == "doc-xyz"' in expr


def test_delete_document_rejects_bad_ids_before_touching_client():
    client = FakeClient()
    with pytest.raises(ValueError):
        delete_document(client, 't" OR 1=1 --', "doc1")
    assert client.calls == []
