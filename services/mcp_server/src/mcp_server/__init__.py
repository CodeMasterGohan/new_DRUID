"""new_DRUID MCP server — Streamable HTTP + stdio transports, 8 scoped tools."""

from .server import create_app, build_fastmcp_for_tests, Settings
from .tools import TOOLS, ServiceDeps
from .context import RequestContext, get_context
from .clients import (
    RetrievalServiceClient,
    IngestionServiceClient,
    CatalogClient,
)

__all__ = [
    "create_app",
    "build_fastmcp_for_tests",
    "Settings",
    "TOOLS",
    "ServiceDeps",
    "RequestContext",
    "get_context",
    "RetrievalServiceClient",
    "IngestionServiceClient",
    "CatalogClient",
]
