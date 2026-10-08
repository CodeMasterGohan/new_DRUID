"""stdio adapter tests: the trusted credential context is explicit and
fail-closed; gateway concepts do not leak into the stdio path."""

from __future__ import annotations

import pytest

from mcp_server.stdio_adapter import build_trusted_context, parse_args


def test_trusted_context_from_args():
    args = parse_args(
        ["--user-id", "u1", "--group-id", "acme", "--roles", "member,reviewer"]
    )
    ctx = build_trusted_context(args)
    assert ctx.user_id == "u1"
    assert ctx.group_id == "acme"
    assert ctx.roles == frozenset({"member", "reviewer"})
    assert ctx.request_id  # server-side generated


def test_missing_identity_fails_closed():
    with pytest.raises(SystemExit):
        build_trusted_context(parse_args(["--group-id", "acme"]))
    with pytest.raises(SystemExit):
        build_trusted_context(parse_args(["--user-id", "u1"]))
    with pytest.raises(SystemExit):
        build_trusted_context(
            parse_args(["--user-id", "u1", "--group-id", "acme", "--roles", " , "])
        )


def test_env_var_credentials(monkeypatch):
    monkeypatch.setenv("DRUID_STDIO_USER_ID", "env-user")
    monkeypatch.setenv("DRUID_STDIO_GROUP_ID", "env-group")
    ctx = build_trusted_context(parse_args([]))
    assert ctx.user_id == "env-user"
    assert ctx.group_id == "env-group"
