"""milvus_setup — one-shot Milvus provisioning for new_DRUID (spec §9, v2.1)."""

from .schema import (
    BM25_FUNCTION_NAME,
    COLLECTION_ALIAS,
    COLLECTION_NAME,
    DENSE_DIM,
    PARTITION_KEY_FIELD,
)

__all__ = [
    "BM25_FUNCTION_NAME",
    "COLLECTION_ALIAS",
    "COLLECTION_NAME",
    "DENSE_DIM",
    "PARTITION_KEY_FIELD",
]
