"""Shared fixtures for MCP server contract tests."""

from __future__ import annotations

import pytest

from mcp_server.auth import JWTVerifier
from mcp_server.clients import (
    InMemoryCatalogClient,
    InMemoryIngestionClient,
    InMemoryRetrievalClient,
    RawPassage,
)
from mcp_server.context import RequestContext, clear_context, new_request_id, set_context
from mcp_server.models import PassageProvenance
from mcp_server.server import build_fastmcp_for_tests
from mcp_server.tools import ServiceDeps

SECRET = "test-secret-for-contracts-only"
ISSUER = "druid-gateway"
AUDIENCE = "druid-mcp"


def make_context(*, user_id="u1", group_id="acme", roles=("member",)) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        group_id=group_id,
        roles=frozenset(roles),
        request_id=new_request_id(),
    )


def make_verifier(**overrides) -> JWTVerifier:
    kwargs = {
        "key": SECRET,
        "algorithms": ("HS256",),
        "issuer": ISSUER,
        "audience": AUDIENCE,
    }
    kwargs.update(overrides)
    return JWTVerifier(**kwargs)


def sample_passage(**overrides) -> RawPassage:
    prov = {
        "document_id": "doc_acme_1",
        "filename": "Acme Corp 10-K 2025.pdf",
        "chunk_id": "doc_acme_1_c3",
        "page_start": 2,
        "page_end": 3,
        "modality": "text",
        "parser": "olmocr-routed",
        "source_reference": "acme://documents/doc_acme_1#pages=2-3",
        "page_image_ref": None,
    }
    prov.update(overrides)
    return RawPassage(
        provenance=PassageProvenance(**prov),
        text="The quick brown fox.",
    )


@pytest.fixture()
def deps() -> ServiceDeps:
    return ServiceDeps(
        retrieval=InMemoryRetrievalClient([sample_passage()]),
        ingestion=InMemoryIngestionClient(),
        catalog=InMemoryCatalogClient(),
    )


@pytest.fixture()
def member_ctx():
    ctx = make_context(roles=("member",))
    token = set_context(ctx)
    yield ctx
    clear_context(token)


@pytest.fixture()
def reviewer_ctx():
    ctx = make_context(roles=("reviewer",))
    token = set_context(ctx)
    yield ctx
    clear_context(token)


@pytest.fixture()
def fastmcp(deps):
    return build_fastmcp_for_tests(deps)
