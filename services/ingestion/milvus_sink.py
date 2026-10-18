"""Production Milvus index sink for RQ ingestion and deletion.

No synthetic success: the reported count comes from a real Milvus upsert.
The tenant is always part of a delete filter.
"""
from __future__ import annotations

import os

from .pipeline import IndexSink


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class MilvusIndexSink(IndexSink):
    def __init__(self, uri: str, collection: str, client=None):
        if not uri or not collection:
            raise RuntimeError("MILVUS_URI and MILVUS_ALIAS are required")
        if client is None:
            from pymilvus import MilvusClient
            client = MilvusClient(uri=uri, token=os.environ["MILVUS_TOKEN"]) if os.getenv("MILVUS_TOKEN") else MilvusClient(uri=uri)
        self._client = client
        self._collection = collection

    def index_chunks(self, records: list[dict]) -> int:
        if not records:
            return 0
        result = self._client.upsert(collection_name=self._collection, data=records)
        count = int(result.get("upsert_count", result.get("insert_count", -1)))
        if count != len(records):
            raise RuntimeError(
                f"Milvus upsert count mismatch: {count} != {len(records)}"
            )
        return count

    def delete_document(self, document_id: str, group_id: str) -> int:
        if not document_id or not group_id:
            raise ValueError("tenant and document are required for deletion")
        expr = (f'group_id == "{_escape(group_id)}" and '
                f'document_id == "{_escape(document_id)}"')
        result = self._client.delete(collection_name=self._collection, filter=expr)
        return int(result.get("delete_count", 0))
