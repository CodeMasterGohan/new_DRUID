"""Static deployment invariants; no external dependencies/models required."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_search_service_initializes_real_adapters():
    source = _read("services/retrieval/main.py")
    assert "def build_service()" in source
    assert "wire(build_service())" in source
    assert "MilvusVectorStore(" in source
    assert "SqlAlchemyCatalogPort(" in source
    assert "MiniLMReranker(" in source


def test_ingestion_worker_does_not_implicitly_use_mock_defaults():
    source = _read("services/ingestion/pipeline.py")
    assert source.count("else build_production_deps()") == 2
    assert "indexed != len(records)" in source
    assert "str(version.version_number)" in source
    assert "def build_production_deps()" in source


def test_service_dockerfiles_run_packages_not_top_level_relative_imports():
    for service in ("retrieval", "embedding"):
        dockerfile = _read(f"services/{service}/Dockerfile")
        assert f"services.{service}.main:app" in dockerfile
        assert f"/app/services/{service}/" in dockerfile


def test_shared_backend_network_minimized():
    import yaml
    compose = yaml.safe_load(_read("deploy/docker-compose.yml"))
    internal = ("postgres", "redis", "rustfs", "etcd", "milvus",
                "retrieval", "embedding", "ingestion")
    for name in internal:
        assert "cora" not in compose["services"][name]["networks"], name
    assert "default" in compose["services"]["ingestion"]["networks"]
    assert "druid-net" in compose["services"]["ingestion"]["networks"]


def test_mcp_binds_locally_by_default():
    assert "${MCP_BIND_IP:-127.0.0.1}" in _read("deploy/docker-compose.yml")
