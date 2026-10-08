"""Contract tests: the 8 tool schemas from §11.

- All 8 tools are registered under their exact §11 names.
- Every tool description instructs the caller to treat retrieved text as
  untrusted data, never instructions.
- No tool schema exposes tenant/user/group IDs, arbitrary DB filters,
  filesystem paths, or remote-fetch URLs.
"""

from __future__ import annotations

import pytest

EXPECTED_TOOLS = [
    "search_knowledge_base",
    "ingest_document",
    "get_ingestion_status",
    "list_documents",
    "get_document",
    "get_review_item",
    "resolve_review_item",
    "delete_document",
]

# Parameter-name fragments that must never appear in any tool schema.
FORBIDDEN_FRAGMENTS = (
    "tenant",
    "group_id",
    "user_id",
    "filter",
    "where",
    "sql",
    "path",
    "filepath",
    "file_path",
    "directory",
    "url",
    "uri",
    "href",
    "fetch",
    "download",
)


@pytest.mark.asyncio()
async def test_all_eight_tools_registered(fastmcp):
    tools = await fastmcp.list_tools()
    names = sorted(t.name for t in tools)
    assert names == sorted(EXPECTED_TOOLS), f"unexpected tool set: {names}"


@pytest.mark.asyncio()
async def test_tool_descriptions_instruct_data_not_instructions(fastmcp):
    tools = await fastmcp.list_tools()
    assert len(tools) == 8
    for tool in tools:
        desc = (tool.description or "").lower()
        assert "untrusted" in desc, f"{tool.name}: description must say text is untrusted"
        assert "never" in desc and "instruction" in desc, (
            f"{tool.name}: description must instruct callers never to treat "
            "text as instructions"
        )


@pytest.mark.asyncio()
async def test_no_forbidden_parameters_in_schemas(fastmcp):
    tools = await fastmcp.list_tools()
    for tool in tools:
        props = tool.inputSchema.get("properties", {})
        for param in props:
            lowered = param.lower()
            for frag in FORBIDDEN_FRAGMENTS:
                assert frag not in lowered, (
                    f"{tool.name}: parameter '{param}' looks like a forbidden "
                    f"capability (matched '{frag}')"
                )


@pytest.mark.asyncio()
async def test_expected_parameters_present(fastmcp):
    tools = {t.name: t for t in await fastmcp.list_tools()}
    assert set(tools["search_knowledge_base"].inputSchema["properties"]) >= {
        "query",
        "top_k",
    }
    assert set(tools["ingest_document"].inputSchema["properties"]) >= {
        "filename",
        "mime_type",
        "file_size_bytes",
        "content_sha256",
    }
    assert "job_id" in tools["get_ingestion_status"].inputSchema["properties"]
    assert set(tools["list_documents"].inputSchema["properties"]) >= {
        "review_required",
        "status",
        "limit",
        "offset",
    }
    assert "document_id" in tools["get_document"].inputSchema["properties"]
    assert "review_item_id" in tools["get_review_item"].inputSchema["properties"]
    assert set(tools["resolve_review_item"].inputSchema["properties"]) >= {
        "review_item_id",
        "decision",
        "confirm",
    }
    assert set(tools["delete_document"].inputSchema["properties"]) >= {
        "document_id",
        "confirm",
    }


@pytest.mark.asyncio()
async def test_delete_and_resolve_require_confirm_by_schema(fastmcp):
    """The confirm flag is a real schema parameter (client confirmation)."""
    tools = {t.name: t for t in await fastmcp.list_tools()}
    assert tools["delete_document"].inputSchema["properties"]["confirm"]["type"] == "boolean"
    assert tools["resolve_review_item"].inputSchema["properties"]["confirm"]["type"] == "boolean"
