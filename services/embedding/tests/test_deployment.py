"""The independent application stacks must resolve one shared model server."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
IS_DRUID = (ROOT / "services/ingestion/vllm_backend.py").is_file()


def test_shared_server_network_and_model_contract():
    shared = yaml.safe_load((ROOT / "deploy/docker-compose.embedding.yml").read_text())
    app = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text())
    assert shared["name"] == "shared-embedding"
    server = shared["services"]["vllm-embedding"]
    assert not server.get("ports")
    assert "pooling" in server["command"]
    assert "/models/google/embeddinggemma-2/chat_template.jinja" in server["command"]
    assert server["environment"]["HF_HUB_OFFLINE"] == "1"
    for compose in (app, shared):
        network = compose["networks"]["embedding-net"]
        assert network["external"] is True
        assert network["name"] == "embedding-net"
    clients = ["embedding"] + (["ingestion"] if IS_DRUID else [])
    for name in clients:
        client = app["services"][name]
        assert "embedding-net" in client["networks"]
        assert client["environment"]["EMBEDDING_BACKEND"] == "${EMBEDDING_BACKEND:-vllm}"
        assert "vllm-embedding:8000/v1" in client["environment"]["EMBEDDING_VLLM_URL"]


def test_sibling_shared_configuration_is_identical_when_present():
    sibling = ROOT.parent / ("new_sage" if IS_DRUID else "new_DRUID")
    for name in ("docker-compose.embedding.yml", "shared-embedding.md", "check_embedding.py"):
        other = sibling / "deploy" / name
        if other.exists():
            assert (ROOT / "deploy" / name).read_bytes() == other.read_bytes()
