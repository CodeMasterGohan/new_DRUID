"""Regression for the pinned Milvus SDK's index-name response."""
from unittest.mock import Mock

import pytest

pytest.importorskip("pymilvus")

from ..create_collection import ensure_indexes


def test_existing_custom_index_names_are_described():
    client = Mock()
    client.list_indexes.return_value = ["custom_dense", "custom_sparse"]
    client.describe_index.side_effect = [
        {"field_name": "dense_vector"}, {"field_name": "sparse_vector"}
    ]
    ensure_indexes(client, "scratch")
    assert client.describe_index.call_count == 2
    client.create_index.assert_not_called()


def test_only_missing_index_is_created():
    client = Mock()
    client.list_indexes.return_value = ["custom_dense"]
    client.describe_index.return_value = {"field_name": "dense_vector"}
    ensure_indexes(client, "scratch")
    client.create_index.assert_called_once()
    _, indexes = client.create_index.call_args.args
    assert {spec["field_name"] for spec in indexes} == {"sparse_vector"}
