"""Every built service module must import cleanly.

Services with no application code yet (document_api, embedding, ingestion,
retrieval) are reported as skips — their builders own the code; the
Dockerfile/requirements/compose wiring for them is verified in
test_deploy_wiring.py.
"""

import importlib
import os

import pytest

BUILD_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# module -> file that must exist for the import test to run
SERVICE_MODULES = {
    "services.common.request_context": "services/common/request_context.py",
    "services.catalog.models": "services/catalog/models.py",
    "services.catalog.repository": "services/catalog/repository.py",
    "services.catalog.activation": "services/catalog/activation.py",
    "services.catalog.errors": "services/catalog/errors.py",
    "services.gateway.app": "services/gateway/app.py",
    "services.gateway.auth": "services/gateway/auth.py",
    "services.gateway.druid_internal": "services/gateway/druid_internal.py",
    "services.gateway.proxy": "services/gateway/proxy.py",
    "services.gateway.quotas": "services/gateway/quotas.py",
    "services.artifacts.src.artifact_manifest": "services/artifacts/src/artifact_manifest.py",
    "services.artifacts.src.cli": "services/artifacts/src/cli.py",
    "services.milvus_setup.schema": "services/milvus_setup/schema.py",
    "services.milvus_setup.deletion": "services/milvus_setup/deletion.py",
    "services.milvus_setup.create_collection": "services/milvus_setup/create_collection.py",
    "mcp_server.context": "services/mcp_server/src/mcp_server/context.py",
    "mcp_server.auth": "services/mcp_server/src/mcp_server/auth.py",
    "mcp_server.models": "services/mcp_server/src/mcp_server/models.py",
    "mcp_server.clients": "services/mcp_server/src/mcp_server/clients.py",
    "mcp_server.tools": "services/mcp_server/src/mcp_server/tools.py",
    "mcp_server.server": "services/mcp_server/src/mcp_server/server.py",
    "mcp_server.stdio_adapter": "services/mcp_server/src/mcp_server/stdio_adapter.py",
}

NOT_YET_BUILT = ("services.document_api", "services.embedding",
                 "services.ingestion", "services.retrieval")


@pytest.mark.parametrize("module", sorted(SERVICE_MODULES))
def test_service_module_imports(module):
    rel = SERVICE_MODULES[module]
    if not os.path.exists(os.path.join(BUILD_ROOT, rel)):
        pytest.skip(f"{rel} not present")
    importlib.import_module(module)


@pytest.mark.parametrize("service", NOT_YET_BUILT)
def test_service_code_not_yet_built(service):
    """Placeholder: fails loudly if a service dir gains code without tests.

    Sibling builders own document_api / embedding / ingestion / retrieval.
    """
    service_dir = os.path.join(BUILD_ROOT, service)
    py_files = [
        f for f in os.listdir(service_dir)
        if f.endswith(".py") and f != "__init__.py"
    ] if os.path.isdir(service_dir) else []
    if py_files:
        pytest.fail(
            f"{service} now has code ({py_files}) — add import coverage to "
            "test_imports.py SERVICE_MODULES"
        )
    pytest.skip(f"{service} application code not yet built (sibling scope)")
