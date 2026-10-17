"""Contract tests for DRUID natural-language skill and its safe end-user CLI."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "druid-natural-language"
SCRIPT = SKILL / "scripts" / "druid_user.py"
spec = importlib.util.spec_from_file_location("druid_user_skill_client", SCRIPT)
client = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(client)


def test_skill_contains_entrypoint_router_and_contracts():
    skill = (SKILL / "SKILL.md").read_text()
    router = (SKILL / "references" / "intent-routing.md").read_text()
    workflows = (SKILL / "references" / "api-workflows.md").read_text()
    examples = (SKILL / "references" / "examples.md").read_text()
    assert skill.startswith("---\nname: druid-natural-language\n")
    for path in ("references/intent-routing.md", "references/api-workflows.md",
                 "references/examples.md", "scripts/druid_user.py"):
        assert path in skill
    for operation in ("search_knowledge_base", "/v1/documents/upload",
                      "/v1/jobs/", "X-Group-Selection", "/v1/identity/me"):
        assert operation in skill + router + workflows
    for negative in ("prompt-injection", "Ambiguous deletion",
                     "Multi-group", "Service request"):
        assert negative.lower() in examples.lower()


def test_refuses_token_on_public_http_or_redirected_origin(monkeypatch):
    for untrusted in (
        "http://druid.example.invalid",
        "https://other.invalid/some/path",
        "https://user:secret@druid.example.invalid",
        "https://druid.example.invalid/?redirect=bad",
    ):
        monkeypatch.setenv("DRUID_API_BASE", untrusted)
        monkeypatch.delenv("DRUID_ALLOW_LOCAL_HTTP", raising=False)
        with pytest.raises(client.DruidError):
            client._origin()
    monkeypatch.setenv("DRUID_API_BASE", "https://druid.example.invalid")
    assert client._origin() == "https://druid.example.invalid"


def test_group_selection_requires_membership_and_explicit_writes():
    me = {"groups": [{"name": "engineering"}, {"name": "research"}],
          "is_system_admin": False}
    with pytest.raises(client.DruidError, match="multiple"):
        client._select_group(me, "", all_groups=False)
    assert client._select_group(me, "", all_groups=True) == ["engineering", "research"]
    assert client._select_group(me, "research", all_groups=False) == ["research"]
    with pytest.raises(client.DruidError, match="not in"):
        client._select_group(me, "finance", all_groups=False)
    assert client._select_group({"groups": [{"name": "single"}]},
                                "", all_groups=False) == ["single"]


def test_sse_json_rpc_envelope_and_error_propagation():
    wrapped = {"jsonrpc": "2.0", "id": 1,
               "result": {"content": [{"type": "text",
                                      "text": json.dumps({"passages": [{
                                          "text": "hello",
                                          "provenance": {"filename": "guide.pdf"},
                                      }]})}]}}
    event = 'event: message\ndata: ' + json.dumps(wrapped) + '\n\n'
    assert client._decode_json_or_sse(event.encode(), "text/event-stream") == wrapped
    assert client._decode_json_or_sse(json.dumps(wrapped).encode(),
                                      "application/json") == wrapped


def test_mcp_search_uses_real_tool_args_and_group(monkeypatch):
    seen = {}
    def fake(method, path, **kwargs):
        seen.update(method=method, path=path, **kwargs)
        return {"result": {"content": [{"type": "text",
            "text": json.dumps({"passages": [{"text": "hello"}]})}]}}
    monkeypatch.setattr(client, "_call", fake)
    result = client._mcp("search_knowledge_base",
                         {"query": "test", "top_k": 8}, "engineering")
    assert result["passages"][0]["text"] == "hello"
    assert seen["path"] == "/mcp"
    assert seen["group"] == "engineering"
    assert seen["json_body"]["params"]["name"] == "search_knowledge_base"
    assert seen["json_body"]["params"]["arguments"]["query"] == "test"


def test_client_enforces_explicit_confirmation_before_any_delete(monkeypatch, capsys):
    monkeypatch.setattr(client, "_identity",
                        lambda: {"groups": [{"name": "engineering"}]})
    def fail_network(*args, **kwargs):
        raise AssertionError("no API mutation should be attempted")
    monkeypatch.setattr(client, "_call", fail_network)
    with pytest.raises(client.DruidError, match="requires --confirm"):
        client.main(["delete", "document-123"])
    with pytest.raises(client.DruidError, match="requires --confirm"):
        client.main(["review-resolve", "review-123", "rejected"])


def test_multipart_upload_uses_exact_document_api_file_field(tmp_path):
    path = tmp_path / "handbook.pdf"
    path.write_bytes(b"%PDF-1.7\nThis is only a fixture")
    body, content_type = client._multipart(path)
    assert content_type.startswith("multipart/form-data; boundary=druid-")
    assert b'name="file"; filename="handbook.pdf"' in body
    assert b"Content-Type: application/pdf" in body
    assert b"%PDF-1.7" in body
    unsupported = tmp_path / "payload.exe"
    unsupported.write_bytes(b"test")
    with pytest.raises(client.DruidError):
        client._multipart(unsupported)


def test_read_only_cross_group_search_is_executed_group_by_group(monkeypatch, capsys):
    monkeypatch.setattr(client, "_identity", lambda: {
        "groups": [{"name": "engineering"}, {"name": "research"}]
    })
    calls = []
    def fake_mcp(tool, args, group):
        calls.append((tool, args, group))
        return {"passages": []}
    monkeypatch.setattr(client, "_mcp", fake_mcp)
    client.main(["--all-groups", "search", "how do we onboard?"])
    output = json.loads(capsys.readouterr().out)
    assert [x["group"] for x in output] == ["engineering", "research"]
    assert [c[2] for c in calls] == ["engineering", "research"]


def test_document_delete_queues_job_in_requested_group(monkeypatch, capsys):
    monkeypatch.setattr(client, "_identity", lambda: {
        "groups": [{"name": "engineering"}]
    })
    calls = []
    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"document_id": "doc-123", "job_id": "job-123", "status": "DELETING"}
    monkeypatch.setattr(client, "_call", fake_request)
    client.main(["--group", "engineering", "--confirm", "delete", "doc-123"])
    assert calls[0][0:2] == ("DELETE", "/v1/documents/doc-123")
    assert calls[0][2]["group"] == "engineering"
    assert json.loads(capsys.readouterr().out)["result"]["job_id"] == "job-123"
