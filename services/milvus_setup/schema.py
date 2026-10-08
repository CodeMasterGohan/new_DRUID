"""Milvus schema definition for new_DRUID — spec §9 (v2.1).

This module is the single source of truth for the collection layout. The
schema is expressed as plain data (``FIELD_SPECS`` / ``BM25_FUNCTION_SPEC`` /
``INDEX_SPECS``) so tests and tooling can inspect it without importing
pymilvus; :func:`build_pymilvus_schema` / :func:`build_index_params`
translate it into the pinned pymilvus SDK (2.5.x).

Versioned physical collection + stable alias (spec §9)::

    collection: "project_knowledge_base_v1"
    alias:      "project_knowledge_base"
    partition key field: "group_id"

Dense vectors live in one unified 768-dim space shared by text chunks and
page-image records (v2.1: EmbeddingGemma 2 text+image). ``metadata.modality``
is ``"text"`` or ``"page_image"``.
"""

from __future__ import annotations

COLLECTION_NAME = "project_knowledge_base_v1"
COLLECTION_ALIAS = "project_knowledge_base"
PARTITION_KEY_FIELD = "group_id"

DENSE_DIM = 768
BM25_FUNCTION_NAME = "text_to_bm25"

# Opaque ID contract shared with the MCP server / catalog: ^[A-Za-z0-9_-]{1,128}$
ID_MAX_LENGTH = 128

# Plain-data field specs: (name, dtype-name, kwargs). dtype names map to
# pymilvus.DataType members in build_pymilvus_schema().
FIELD_SPECS: tuple[dict, ...] = (
    {"name": "chunk_id", "dtype": "VARCHAR", "max_length": ID_MAX_LENGTH,
     "is_primary": True, "auto_id": False,
     "description": "Tenant-scoped unique chunk ID (primary key)"},
    {"name": "group_id", "dtype": "VARCHAR", "max_length": ID_MAX_LENGTH,
     "is_partition_key": True,
     "description": "Tenant ownership (partition key)"},
    {"name": "document_id", "dtype": "VARCHAR", "max_length": ID_MAX_LENGTH,
     "description": "Document lookup and deletion"},
    {"name": "document_version", "dtype": "VARCHAR", "max_length": 64,
     "description": "Version-scoped operations"},
    {"name": "parent_id", "dtype": "VARCHAR", "max_length": ID_MAX_LENGTH,
     "nullable": True,
     "description": "Context expansion (parent chunk)"},
    {"name": "page_start", "dtype": "INT64",
     "description": "Source attribution: first page (1-based)"},
    {"name": "page_end", "dtype": "INT64",
     "description": "Source attribution: last page (1-based)"},
    {"name": "chunk_order", "dtype": "INT64",
     "description": "Neighbor ordering within the document version"},
    {"name": "text", "dtype": "VARCHAR", "max_length": 65535,
     "enable_analyzer": True, "analyzer_params": {"tokenizer": "standard"},
     "description": "Retrieval text (analyzer-enabled for BM25)"},
    {"name": "identifiers", "dtype": "ARRAY", "element_type": "VARCHAR",
     "max_capacity": 64, "max_length": 256,
     "description": "Exact-match identifiers (invoice nos., part numbers, clause refs)"},
    {"name": "dense_vector", "dtype": "FLOAT_VECTOR", "dim": DENSE_DIM,
     "description": "Semantic retrieval — unified text/page-image space"},
    {"name": "sparse_vector", "dtype": "SPARSE_FLOAT_VECTOR",
     "description": "BM25-generated sparse representation"},
    {"name": "metadata", "dtype": "JSON",
     "description": "Provenance incl. modality: text | page_image"},
)

# BM25 function spec (spec §9): explicit function, text -> sparse_vector.
# Sparse index must use the BM25 metric, NOT IP.
BM25_FUNCTION_SPEC: dict = {
    "name": BM25_FUNCTION_NAME,
    "function_type": "BM25",
    "input_field_names": ["text"],
    "output_field_names": ["sparse_vector"],
}

INDEX_SPECS: tuple[dict, ...] = (
    {"field_name": "dense_vector", "index_type": "HNSW", "metric_type": "COSINE",
     "params": {"M": 16, "efConstruction": 200}},
    {"field_name": "sparse_vector", "index_type": "SPARSE_INVERTED_INDEX",
     "metric_type": "BM25", "params": {}},
)

# Optional HNSW optimization — spec §9: test-first, NEVER the authorization
# boundary. Off by default; enabled explicitly via --enable-partition-isolation
# after compatibility testing with the pinned Milvus version.
PARTITION_ISOLATION_PROPERTY = "partitionkey.isolation"


def field_names() -> list[str]:
    return [f["name"] for f in FIELD_SPECS]


def build_pymilvus_schema():
    """Translate FIELD_SPECS into a pymilvus CollectionSchema (lazy import)."""
    from pymilvus import CollectionSchema, DataType, FieldSchema, Function, FunctionType

    fields = []
    for spec in FIELD_SPECS:
        dtype = DataType[spec["dtype"]]
        kwargs: dict = {"description": spec.get("description", "")}
        for key in ("max_length", "dim", "is_primary", "auto_id",
                    "is_partition_key", "nullable", "enable_analyzer",
                    "analyzer_params", "max_capacity"):
            if key in spec:
                kwargs[key] = spec[key]
        if spec["dtype"] == "ARRAY":
            kwargs["element_type"] = DataType[spec["element_type"]]
        fields.append(FieldSchema(name=spec["name"], dtype=dtype, **kwargs))

    fn_spec = BM25_FUNCTION_SPEC
    functions = [Function(
        name=fn_spec["name"],
        function_type=FunctionType[fn_spec["function_type"]],
        input_field_names=fn_spec["input_field_names"],
        output_field_names=fn_spec["output_field_names"],
    )]
    return CollectionSchema(
        fields,
        functions=functions,
        description="new_DRUID project knowledge base (spec §9 v2.1)",
        partition_key_field=PARTITION_KEY_FIELD,
        enable_dynamic_field=False,
    )


def build_index_params():
    """Translate INDEX_SPECS into a pymilvus IndexParams (lazy import)."""
    from pymilvus.milvus_client.index import IndexParams

    index_params = IndexParams()
    for spec in INDEX_SPECS:
        index_params.add_index(
            field_name=spec["field_name"],
            index_type=spec["index_type"],
            metric_type=spec["metric_type"],
            params=spec["params"],
        )
    return index_params
