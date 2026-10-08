"""Tenant authorization predicates (spec §4).

One predicate object is built per search and passed — identically — to the
dense, BM25, and identifier branches. Authorization lives in the
application repository layer, never in Milvus partition behavior.
"""

from __future__ import annotations

from dataclasses import dataclass


def _escape_milvus_string(value: str) -> str:
    # Milvus filter expressions use double-quoted strings; escape
    # backslashes and quotes so caller data can never break out.
    return value.replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True)
class AuthPredicate:
    """Authorization predicate for one search.

    ``group_id`` is the mandatory tenant predicate. ``allowed_versions`` is
    the catalog hard filter from §10 step 3: the set of
    ``(document_id, document_version)`` string keys that are active, READY,
    and unreviewed-clean for this tenant. ``None`` means "whole tenant"
    (used only when the catalog port cannot enumerate versions — the
    default port always enumerates, so this is a defensive fallback).
    """

    group_id: str
    allowed_versions: frozenset[tuple[str, str]] | None

    def to_milvus_expr(self) -> str:
        """Render as a Milvus filter expression.

        Raises if the allowed set is empty — an empty set must short-circuit
        the search in the service layer instead of hitting the store.
        """
        if self.allowed_versions is not None and not self.allowed_versions:
            raise ValueError(
                "empty allowed_versions: the service must return [] "
                "without querying the vector store"
            )
        group = _escape_milvus_string(self.group_id)
        expr = f'group_id == "{group}"'
        if self.allowed_versions is not None:
            clauses = []
            for doc_id, doc_version in sorted(self.allowed_versions):
                d = _escape_milvus_string(doc_id)
                v = _escape_milvus_string(doc_version)
                clauses.append(f'(document_id == "{d}" and document_version == "{v}")')
            expr += " and (" + " or ".join(clauses) + ")"
        return expr
