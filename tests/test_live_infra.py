"""Live-infrastructure tests — skipped without real backends.

Each test documents the manual/automated gate it represents from spec §13.
Enable selectively in a deploy-like environment, e.g.::

    DRUID_LIVE_POSTGRES=1 DRUID_LIVE_REDIS=1 DRUID_LIVE_MILVUS=1 \\
        pytest tests/test_live_infra.py -v

Nothing here may run (or fail) in CI without the corresponding backend.
"""

import os

import pytest


def _needs(var, what):
    return pytest.mark.skipif(
        not os.environ.get(var), reason=f"requires live {what}: set {var}=1"
    )


@_needs("DRUID_LIVE_POSTGRES", "PostgreSQL")
def test_postgres_alembic_migration_applies_cleanly():
    """§13 operational: alembic upgrade head on a fresh database."""
    pytest.fail("no live PostgreSQL in this environment")


@_needs("DRUID_LIVE_POSTGRES", "PostgreSQL")
def test_postgres_catalog_roundtrip_with_jsonb():
    """JSONB columns (metadata, trusted_roles) round-trip on PostgreSQL."""
    pytest.fail("no live PostgreSQL in this environment")


@_needs("DRUID_LIVE_REDIS", "Redis")
def test_redis_quota_store_lua_scripts():
    """§4 quotas: RedisQuotaStore sliding-window + ingestion accounting."""
    pytest.fail("no live Redis in this environment")


@_needs("DRUID_LIVE_REDIS", "Redis")
def test_redis_rq_ingestion_job_enqueue():
    """Durable ingestion job survives broker round-trip."""
    pytest.fail("no live Redis in this environment")


@_needs("DRUID_LIVE_MILVUS", "Milvus")
def test_milvus_setup_idempotent_end_to_end():
    """create_collection twice; schema verify; alias stable."""
    pytest.fail("no live Milvus in this environment")


@_needs("DRUID_LIVE_MILVUS", "Milvus")
def test_milvus_hybrid_search_recall_smoke():
    """Insert synthetic chunks; dense+BM25+identifier all return them."""
    pytest.fail("no live Milvus in this environment")


@_needs("DRUID_LIVE_VLLM", "vLLM on H200")
def test_vllm_olmocr_fp8_smoke():
    """olmOCR-2-7B-FP8 serves a page render; output parses as expected."""
    pytest.fail("no GPU in this environment")


@_needs("DRUID_LIVE_VLLM", "vLLM on H200")
def test_gpu_arbitration_under_load():
    """§13: olmOCR throughput holds with embedding/query services loaded."""
    pytest.fail("no GPU in this environment")
