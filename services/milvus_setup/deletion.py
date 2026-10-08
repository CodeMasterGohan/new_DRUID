"""Tenant-scoped Milvus deletion helpers — spec §12.

Deletion is authorized in the catalog first; these helpers execute the
tenant-scoped Milvus side. They always operate through the stable alias
(never a hardcoded versioned collection name) and always include the
tenant predicate — there is no code path that deletes without ``group_id``.

Note on visibility: Milvus deletes go through compaction, so a deleted
chunk can remain briefly visible in the index. Retrieval must ALSO apply
the catalog-layer hard filter (§10 step 3); this module only guarantees
the delete is issued with the correct tenant scope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .schema import COLLECTION_ALIAS

# Opaque ID contract (same as MCP server / catalog): caller-supplied IDs that
# do not match are rejected instead of being interpolated into an expression.
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def build_delete_expr(group_id: str, document_id: str) -> str:
    """Build a tenant-scoped delete expression.

    Raises:
        ValueError: if either ID fails the opaque-ID contract. Fail closed —
            never interpolate unvalidated input into a filter expression.
    """
    for label, value in (("group_id", group_id), ("document_id", document_id)):
        if not isinstance(value, str) or not _OPAQUE_ID_RE.match(value):
            raise ValueError(
                f"invalid {label}: must match ^[A-Za-z0-9_-]{{1,128}}$; "
                "refusing to build a delete expression"
            )
    # The charset above contains no quotes or backslashes, so interpolation
    # into a double-quoted Milvus string literal is safe.
    return f'group_id == "{group_id}" and document_id == "{document_id}"'


@dataclass
class DeleteResult:
    """Outcome of a tenant-scoped document deletion from Milvus."""

    group_id: str
    document_id: str
    collection_alias: str
    delete_count: int
    expr: str


def delete_document(
    client,
    group_id: str,
    document_id: str,
    collection_alias: str = COLLECTION_ALIAS,
) -> DeleteResult:
    """Delete every chunk of ``document_id`` owned by ``group_id``.

    Args:
        client: a connected ``pymilvus.MilvusClient`` (or a test double
            exposing ``delete(collection_name, filter)`` returning an object
            with ``delete_count``).
        group_id: trusted tenant ID from the validated request context —
            never a caller-supplied value that bypassed the catalog.
        document_id: opaque document ID (validated against the ID contract).
        collection_alias: Milvus alias to delete through (default: the
            stable ``project_knowledge_base`` alias).

    Returns:
        DeleteResult with the server-reported delete count.
    """
    expr = build_delete_expr(group_id, document_id)
    result = client.delete(collection_name=collection_alias, filter=expr)
    count = result.get("delete_count", 0) if isinstance(result, dict) else getattr(
        result, "delete_count", 0
    )
    return DeleteResult(
        group_id=group_id,
        document_id=document_id,
        collection_alias=collection_alias,
        delete_count=int(count),
        expr=expr,
    )
