"""Build-root integration tests: exercise the seams between services.

These tests verify cross-service contracts WITHOUT live infrastructure:
no PostgreSQL, Redis, Milvus, or GPU is required. Tests that need live
infra live in test_live_infra.py and are skipped by default.

Run from the build root:  python -m pytest tests -q
"""

import os
import sys

BUILD_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BUILD_ROOT not in sys.path:
    sys.path.insert(0, BUILD_ROOT)

# The MCP server uses a src/ layout.
MCP_SRC = os.path.join(BUILD_ROOT, "services", "mcp_server", "src")
if MCP_SRC not in sys.path:
    sys.path.insert(0, MCP_SRC)
