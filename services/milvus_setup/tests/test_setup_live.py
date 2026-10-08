"""Live-Milvus tests — skipped without a running Milvus.

Run against a real Milvus (e.g. the compose ``milvus`` service) with::

    MILVUS_URI=http://localhost:19530 pytest services/milvus_setup/tests/test_setup_live.py -v  # noqa: E501

These are intentionally destructive-free EXCEPT the recreate path, which is
NOT exercised here (it drops data).
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DRUID_LIVE_MILVUS"),
    reason="requires live Milvus: set DRUID_LIVE_MILVUS=1 and MILVUS_URI",
)


def test_live_idempotent_setup():
    pytest.fail("live Milvus not available in this environment")


def test_live_alias_cutover_and_rollback():
    pytest.fail("live Milvus not available in this environment")


def test_live_tenant_scoped_delete():
    pytest.fail("live Milvus not available in this environment")


def test_live_partition_isolation_flag():
    pytest.fail("live Milvus not available in this environment")
