#!/usr/bin/env python3
"""Compose image override for immutable GitLab registry releases.

No YAML libraries. Image tag/registry/name validation prevents injection;
infrastructure images remain pinned by the project's main Compose file.
"""
from __future__ import annotations
import argparse
import re
import sys

SERVICES = {
    "druid": {
        "catalog": "catalog",
        "milvus_setup": "milvus_setup",
        "gateway": "gateway",
        "document_api": "document_api",
        "mcp_server": "mcp_server",
        "web_ui": "web_ui",
        "embedding": "embedding",
        "retrieval": "retrieval",
        "ingestion": "ingestion",
    },
    "sage": {
        "catalog-migrate": "catalog",
        "milvus_setup": "milvus_setup",
        "embedding": "embedding",
        "retrieval": "retrieval",
        "mcp-server": "mcp_server",
        "ingestion-worker": "ingestion",
        "crawler-worker": "crawler",
        "scheduler": "scheduler",
        "devdocs-scheduler": "scheduler",
    },
}

def render(project: str, registry: str, sha: str, rust_sage: bool = False) -> str:
    if project not in SERVICES:
        raise ValueError("unknown project (druid/sage)")
    if not re.fullmatch(r"[a-zA-Z0-9._:/-]+", registry) or not registry or registry.endswith("/"):
        raise ValueError("invalid image repository name")
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("tag must be the complete immutable 40-character commit SHA")
    if rust_sage and project != "sage":
        raise ValueError("the Rust image variant exists only for Sage")
    lines = ["# Generated from an immutable GitLab commit; do not edit", "services:"]
    for service, image in SERVICES[project].items():
        suffix = "-rust" if rust_sage and image in ("ingestion", "crawler") else ""
        lines += [f"  {service}:", f"    image: {registry}/{image}{suffix}:{sha}"]
    return "\n".join(lines) + "\n"

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("project", choices=sorted(SERVICES))
    p.add_argument("registry")
    p.add_argument("sha")
    p.add_argument("--rust-sage", action="store_true")
    args = p.parse_args(argv)
    sys.stdout.write(render(args.project, args.registry, args.sha, args.rust_sage))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
