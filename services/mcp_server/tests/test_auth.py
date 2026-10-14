"""Auth tests: JWT verification, role scopes, fail-closed context (§4, §11)."""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest

from mcp_server.auth import (
    AuthError,
    ToolAccessDenied,
    check_tool_role,
    context_from_claims,
)
from mcp_server.context import MissingContextError, get_context

from .conftest import AUDIENCE, ISSUER, SECRET, make_context, make_verifier


def mint(*, secret=SECRET, issuer=ISSUER, audience=AUDIENCE, exp_offset=3600,
         extra_claims=None, drop=()):
    payload = {
        "sub": "u1",
        "grp": "acme",
        "roles": ["member"],
        "iss": issuer,
        "aud": audience,
        "exp": int(time.time()) + exp_offset,
    }
    if extra_claims:
        payload.update(extra_claims)
    for claim in drop:
        payload.pop(claim, None)
    return pyjwt.encode(payload, secret, algorithm="HS256")


def test_verifier_accepts_valid_token():
    claims = make_verifier().verify(mint())
    assert claims.subject == "u1"
    assert claims.group_id == "acme"
    assert claims.roles == frozenset({"member"})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"secret": "wrong-secret"},
        {"issuer": "https://evil.example"},
        {"audience": "other-api"},
        {"exp_offset": -3600},
    ],
)
def test_verifier_rejects_bad_tokens(kwargs):
    with pytest.raises(AuthError):
        make_verifier().verify(mint(**kwargs))


def test_verifier_rejects_missing_claims():
    with pytest.raises(AuthError):
        make_verifier().verify(mint(drop=("sub",)))
    with pytest.raises(AuthError):
        make_verifier().verify(mint(drop=("grp",)))
    with pytest.raises(AuthError):
        make_verifier().verify(mint(extra_claims={"roles": []}))
    with pytest.raises(AuthError):
        make_verifier().verify("")


def test_context_from_claims_generates_request_id():
    ctx = context_from_claims(make_verifier().verify(mint()))
    assert ctx.user_id == "u1" and ctx.group_id == "acme"
    assert ctx.request_id  # server-side, never caller-supplied


def test_missing_context_fails_closed():
    with pytest.raises(MissingContextError):
        get_context()


def test_tool_role_enforcement():
    member = make_context(roles=("member",))
    reviewer = make_context(roles=("reviewer",))
    admin = make_context(roles=("admin",))

    check_tool_role("search_knowledge_base", member)  # any authenticated role
    check_tool_role("resolve_review_item", reviewer)
    check_tool_role("resolve_review_item", admin)  # admin is a superset

    user = make_context(roles=("user",))
    unknown = make_context(roles=("reader",))  # not in the role vocabulary
    for tool in ("search_knowledge_base", "ingest_document", "list_documents",
                 "get_document", "get_ingestion_status", "delete_document"):
        check_tool_role(tool, user)
        with pytest.raises(ToolAccessDenied):
            check_tool_role(tool, unknown)  # unknown roles fail closed

    with pytest.raises(ToolAccessDenied):
        check_tool_role("resolve_review_item", member)
    with pytest.raises(ToolAccessDenied):
        check_tool_role("resolve_review_item", user)
    with pytest.raises(ToolAccessDenied):
        check_tool_role("get_review_item", user)
    with pytest.raises(ToolAccessDenied):
        check_tool_role("get_review_item", member)
    with pytest.raises(ToolAccessDenied):
        check_tool_role("no_such_tool", admin)


async def test_tool_call_without_context_fails(deps):
    """Calling a tool impl with no context installed must fail closed."""
    import mcp_server.context as ctx_mod
    from mcp_server import tools as tool_impls
    from mcp_server.models import SearchArgs

    ctx_mod._current.set(None)  # guarantee no context, regardless of fixtures
    with pytest.raises(MissingContextError):
        await tool_impls.search_knowledge_base(deps, SearchArgs(query="x"))


async def test_resolve_review_item_denied_for_member(deps, member_ctx):
    from mcp_server import tools as tool_impls
    from mcp_server.models import ResolveReviewItemArgs

    with pytest.raises(ToolAccessDenied):
        await tool_impls.resolve_review_item(
            deps,
            ResolveReviewItemArgs(review_item_id="r1", decision="accept", confirm=True),
        )


async def test_resolve_review_item_requires_confirm(deps, reviewer_ctx):
    from mcp_server import tools as tool_impls
    from mcp_server.models import ResolveReviewItemArgs

    result = await tool_impls.resolve_review_item(
        deps,
        ResolveReviewItemArgs(review_item_id="r1", decision="accept", confirm=False),
    )
    assert result["applied"] is False
    assert "confirm=true" in result["reason"]


async def test_delete_requires_confirm(deps, member_ctx):
    from mcp_server import tools as tool_impls
    from mcp_server.models import DeleteDocumentArgs

    result = await tool_impls.delete_document(
        deps, DeleteDocumentArgs(document_id="doc_1", confirm=False)
    )
    assert result["applied"] is False
    # the catalog must not have been touched
    assert deps.catalog.deleted == []
