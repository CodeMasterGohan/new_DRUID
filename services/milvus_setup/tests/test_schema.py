"""Schema-definition tests — no live Milvus required.

The schema is expressed as plain data in schema.py, so most of this suite
runs without pymilvus. Tests that translate to the pinned SDK are skipped
when pymilvus is not installed.
"""

import pytest

from .. import schema as S

try:
    import pymilvus  # noqa: F401

    HAS_PYMILVUS = True
except ImportError:
    HAS_PYMILVUS = False

needs_pymilvus = pytest.mark.skipif(not HAS_PYMILVUS, reason="pymilvus not installed")


def test_collection_naming_matches_spec():
    assert S.COLLECTION_NAME == "project_knowledge_base_v1"
    assert S.COLLECTION_ALIAS == "project_knowledge_base"
    assert S.PARTITION_KEY_FIELD == "group_id"


def test_field_inventory_matches_spec_section_9():
    names = S.field_names()
    for expected in (
        "chunk_id", "group_id", "document_id", "document_version", "parent_id",
        "page_start", "page_end", "chunk_order", "text", "identifiers",
        "dense_vector", "sparse_vector", "metadata",
    ):
        assert expected in names, f"missing spec §9 field: {expected}"
    assert len(names) == len(set(names)), "duplicate field names"


def test_primary_key_and_partition_key():
    by_name = {f["name"]: f for f in S.FIELD_SPECS}
    assert by_name["chunk_id"].get("is_primary") is True
    assert by_name["chunk_id"].get("auto_id") is False
    assert by_name["group_id"].get("is_partition_key") is True
    # Only one partition-key field is legal.
    pk_fields = [f["name"] for f in S.FIELD_SPECS if f.get("is_partition_key")]
    assert pk_fields == ["group_id"]


def test_text_field_is_analyzer_enabled():
    by_name = {f["name"]: f for f in S.FIELD_SPECS}
    text = by_name["text"]
    assert text.get("enable_analyzer") is True
    assert text["max_length"] == 65535


def test_identifiers_is_varchar_array():
    by_name = {f["name"]: f for f in S.FIELD_SPECS}
    ids = by_name["identifiers"]
    assert ids["dtype"] == "ARRAY"
    assert ids["element_type"] == "VARCHAR"


def test_dense_dim_is_768():
    by_name = {f["name"]: f for f in S.FIELD_SPECS}
    assert by_name["dense_vector"]["dim"] == 768 == S.DENSE_DIM


def test_bm25_function_spec():
    fn = S.BM25_FUNCTION_SPEC
    assert fn["name"] == "text_to_bm25"
    assert fn["input_field_names"] == ["text"]
    assert fn["output_field_names"] == ["sparse_vector"]


def test_index_specs():
    by_name = {s["field_name"]: s for s in S.INDEX_SPECS}
    dense = by_name["dense_vector"]
    assert dense["index_type"] == "HNSW"
    assert dense["metric_type"] == "COSINE"
    assert dense["params"] == {"M": 16, "efConstruction": 200}
    sparse = by_name["sparse_vector"]
    assert sparse["index_type"] == "SPARSE_INVERTED_INDEX"
    # BM25 metric, NOT IP (spec §9).
    assert sparse["metric_type"] == "BM25"


@needs_pymilvus
def test_pymilvus_schema_builds():
    sch = S.build_pymilvus_schema()
    pkf = sch.partition_key_field
    # pymilvus returns the partition-key FieldSchema object, not the name.
    pkf_name = pkf.name if hasattr(pkf, "name") else pkf["name"]
    assert pkf_name == "group_id"
    assert sch.primary_field.name == "chunk_id"
    fn_names = [fn.name for fn in sch.functions]
    assert "text_to_bm25" in fn_names
    text_field = next(f for f in sch.fields if f.name == "text")
    assert text_field.enable_analyzer is True


@needs_pymilvus
def test_pymilvus_index_params_build():
    params = S.build_index_params()
    raw_params = [p.to_dict() if hasattr(p, "to_dict") else p for p in params]
    by_field = {p["field_name"]: p for p in raw_params}
    assert by_field["dense_vector"]["metric_type"] == "COSINE"
    assert by_field["sparse_vector"]["metric_type"] == "BM25"
