"""Every built service module must import cleanly.

Covers all nine services (including document_api, embedding, ingestion,
retrieval). Import paths mirror each service's Docker image layout:
document_api modules import under their top-level names (its image copies
the service flat to /app/), everything else as services.<svc>.*.
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
    # document_api ships flat in its image (COPY services/document_api/ /app/,
    # `uvicorn main:app`), so its modules are imported under their top-level
    # names here — mirroring the image layout. (Build-root conftest.py puts
    # services/document_api/ on sys.path.)
    "main": "services/document_api/main.py",
    "job_queue": "services/document_api/job_queue.py",
    "internal_auth": "services/document_api/internal_auth.py",
    "deps": "services/document_api/deps.py",
    "storage": "services/document_api/storage.py",
    "routes.documents": "services/document_api/routes/documents.py",
    "routes.review": "services/document_api/routes/review.py",
    # embedding (standalone CPU service)
    "services.embedding.main": "services/embedding/main.py",
    "services.embedding.service": "services/embedding/service.py",
    "services.embedding.settings": "services/embedding/settings.py",
    "services.embedding.reuse": "services/embedding/reuse.py",
    # ingestion workers
    "services.ingestion.pipeline": "services/ingestion/pipeline.py",
    "services.ingestion.embeddings": "services/ingestion/embeddings.py",
    "services.ingestion.config": "services/ingestion/config.py",
    "services.ingestion.router": "services/ingestion/router.py",
    "services.ingestion.chunker": "services/ingestion/chunker.py",
    # retrieval service
    "services.retrieval.main": "services/retrieval/main.py",
    "services.retrieval.service": "services/retrieval/service.py",
    "services.retrieval.config": "services/retrieval/config.py",
}


@pytest.mark.parametrize("module", sorted(SERVICE_MODULES))
def test_service_module_imports(module):
    rel = SERVICE_MODULES[module]
    if not os.path.exists(os.path.join(BUILD_ROOT, rel)):
        pytest.skip(f"{rel} not present")
    importlib.import_module(module)
