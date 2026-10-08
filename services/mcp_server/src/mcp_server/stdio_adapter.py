"""Standalone stdio adapter for local use.

This is a SEPARATE transport from the authenticated Streamable HTTP service:

- There is no token validation here. Instead, the operator supplies an
  EXPLICIT trusted credential context on the command line (or via
  ``DRUID_STDIO_*`` environment variables). That context is fixed for the
  lifetime of the process.
- Gateway headers NEVER apply to stdio. There is no HTTP layer at all, so
  there is nothing to spoof — but also no gateway in the loop. Run this
  only as the local user it is intended to act as, on a machine you trust.
- The stdio adapter and the HTTP service must never share a process.

Usage:
    python -m mcp_server.stdio_adapter \\
        --user-id alice --group-id acme --roles member,reviewer
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .clients import (
    InMemoryCatalogClient,
    InMemoryIngestionClient,
    InMemoryRetrievalClient,
)
from .context import RequestContext, new_request_id, set_context
from .server import build_fastmcp_for_tests
from .tools import ServiceDeps


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="new_DRUID MCP stdio adapter")
    p.add_argument("--user-id", default=os.getenv("DRUID_STDIO_USER_ID", ""))
    p.add_argument("--group-id", default=os.getenv("DRUID_STDIO_GROUP_ID", ""))
    p.add_argument(
        "--roles",
        default=os.getenv("DRUID_STDIO_ROLES", "member"),
        help="Comma-separated roles, e.g. 'member,reviewer'",
    )
    p.add_argument(
        "--dev-fake-clients",
        action="store_true",
        default=os.getenv("DRUID_MCP_DEV_FAKE_CLIENTS") == "1",
        help="Use in-memory fake service clients (local dev only).",
    )
    return p.parse_args(argv)


def build_trusted_context(args: argparse.Namespace) -> RequestContext:
    """The explicit trusted credential context for this stdio session.

    Fails closed: user-id and group-id are mandatory, roles must be
    non-empty. Nothing is inferred from the environment beyond these
    explicit values.
    """
    user_id = args.user_id.strip()
    group_id = args.group_id.strip()
    roles = frozenset(r.strip() for r in args.roles.split(",") if r.strip())
    if not user_id:
        raise SystemExit("error: --user-id (or DRUID_STDIO_USER_ID) is required")
    if not group_id:
        raise SystemExit("error: --group-id (or DRUID_STDIO_GROUP_ID) is required")
    if not roles:
        raise SystemExit("error: at least one --roles entry is required")
    return RequestContext(
        user_id=user_id,
        group_id=group_id,
        roles=roles,
        request_id=new_request_id(),
    )


async def _amain(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    ctx = build_trusted_context(args)
    if not args.dev_fake_clients:
        raise SystemExit(
            "error: no service clients configured. Pass --dev-fake-clients for "
            "local development; production deployments must wire real "
            "RetrievalServiceClient / IngestionServiceClient / CatalogClient "
            "implementations before using the stdio adapter."
        )
    deps = ServiceDeps(
        retrieval=InMemoryRetrievalClient(),
        ingestion=InMemoryIngestionClient(),
        catalog=InMemoryCatalogClient(),
    )
    # The trusted context is fixed for the whole stdio session.
    set_context(ctx)
    mcp = build_fastmcp_for_tests(deps)
    await mcp.run_stdio_async()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
