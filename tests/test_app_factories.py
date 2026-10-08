"""App factories must construct: gateway (FastAPI) and MCP server (Starlette).

Covers spec §4 (gateway trusted context) and §11 (MCP transports) at the
wiring level. No network, no backends.
"""

import os

import pytest
from fastapi.testclient import TestClient

from services.gateway.app import GatewaySettings, create_app as create_gateway_app

mcp_server = pytest.importorskip(
    "mcp_server.server", reason="mcp_server not importable"
)


def make_gateway_settings(**overrides):
    kwargs = dict(
        jwt_secret="test-secret",
        jwt_issuer="druid-test",
        jwt_audience="druid-api",
        internal_secret="internal-test-secret",
    )
    kwargs.update(overrides)
    return GatewaySettings(**kwargs)


def test_gateway_app_constructs_and_serves_healthz():
    app = create_gateway_app(make_gateway_settings())
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/healthz")
    assert resp.status_code == 200


def test_gateway_module_level_app_exists_for_uvicorn():
    import services.gateway.app as app_module

    assert hasattr(app_module, "app"), "Dockerfile CMD uses main:app"


def test_mcp_app_constructs_with_fake_clients(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("JWT_ISSUER", "druid-test")
    monkeypatch.setenv("JWT_AUDIENCE", "druid-mcp")
    monkeypatch.setenv("DRUID_MCP_DEV_FAKE_CLIENTS", "1")
    app = mcp_server.create_app()
    assert app is not None


def test_mcp_app_refuses_without_jwt_or_clients(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("JWT_PUBLIC_KEY_PEM", raising=False)
    monkeypatch.setenv("DRUID_MCP_DEV_FAKE_CLIENTS", "1")
    with pytest.raises(RuntimeError):
        mcp_server.create_app()
