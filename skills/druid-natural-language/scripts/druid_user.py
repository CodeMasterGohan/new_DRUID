#!/usr/bin/env python3
"""Authenticated DRUID end-user API CLI for natural-language agent runtimes.

Uses only Python standard library and env-provided Keycloak access token.
Never accepts a token as a command-line arg. Requests use HTTPS; no redirects
are followed with bearer credentials. Mutations never auto-retry.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MIMES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


class DruidError(RuntimeError):
    """A gateway request was denied or could not be completed."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a bearer token to another host or path via redirect.
        return None


def _origin() -> str:
    origin = os.environ.get("DRUID_API_BASE", "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(origin)
    valid_local = (
        os.environ.get("DRUID_ALLOW_LOCAL_HTTP") == "1"
        and parsed.scheme == "http"
        and parsed.hostname in ("127.0.0.1", "localhost", "::1")
    )
    if (not origin or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.path not in ("", "/")
            or (parsed.scheme != "https" and not valid_local)
            or not parsed.hostname):
        raise DruidError(
            "DRUID_API_BASE must be an HTTPS gateway origin "
            "(or opt-in loopback HTTP for local development)"
        )
    return origin


def _token() -> str:
    token = os.environ.get("DRUID_ACCESS_TOKEN", "").strip()
    if not token or any(c in token for c in ("\r", "\n", "\t")):
        raise DruidError("set DRUID_ACCESS_TOKEN from an approved Keycloak session")
    return token


def _encoded(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _decode_json_or_sse(body: bytes, content_type: str) -> object:
    if "text/event-stream" not in content_type:
        return json.loads(body.decode("utf-8"))
    # Streamable HTTP can return multiple event frames. Select the last
    # valid JSON-RPC response, never ignore an explicit error frame.
    last = None
    for frame in body.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            ln[5:].lstrip(" ") for ln in frame.split("\n")
            if ln.startswith("data:")
        )
        if not data or data == "[DONE]":
            continue
        parsed = json.loads(data)
        if isinstance(parsed, dict) and ("error" in parsed or "result" in parsed):
            last = parsed
    if last is None:
        raise DruidError("MCP returned no JSON-RPC response")
    return last


def _call(method: str, path: str, *, group: str = "",
          json_body: dict | None = None, raw: bytes | None = None,
          content_type: str = "", accept: str = "application/json") -> object:
    if not (path.startswith("/v1/") or path == "/mcp"):
        raise DruidError("path is not on the documented DRUID gateway")
    if json_body is not None and raw is not None:
        raise DruidError("cannot send JSON and raw data simultaneously")
    payload = json.dumps(json_body).encode() if json_body is not None else raw
    headers = {
        "Authorization": "Bearer " + _token(),
        "Accept": accept,
    }
    if group:
        if len(group) > 63 or not all(c.isalnum() or c in "_-" for c in group):
            raise DruidError("invalid DRUID group selection")
        headers["X-Group-Selection"] = group
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    elif raw is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(
        _origin() + path, headers=headers, data=payload, method=method
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=45) as response:
            body = response.read(MAX_BODY_BYTES + 1)
            kind = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        # An auth error may contain user info or upstream details. Avoid
        # echoing a full backend body, token, headers or upload ticket.
        raise DruidError(f"DRUID API returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DruidError(
            "gateway connection failed or timed out; mutation outcome "
            "may be unknown, inspect state before retrying"
        ) from exc
    if len(body) > MAX_BODY_BYTES:
        raise DruidError("API response exceeded safety size limit")
    try:
        return _decode_json_or_sse(body, kind) if body else {"ok": True}
    except (ValueError, UnicodeDecodeError) as exc:
        raise DruidError("unexpected API response encoding") from exc


def _identity() -> dict:
    data = _call("GET", "/v1/identity/me")
    if not isinstance(data, dict) or "groups" not in data:
        raise DruidError("identity response did not include DRUID groups")
    return data


def _groups(me: dict) -> list[str]:
    result = []
    for entry in me.get("groups", []):
        if isinstance(entry, dict) and entry.get("name"):
            result.append(entry["name"])
    return list(dict.fromkeys(result))


def _select_group(me: dict, requested: str, *, all_groups: bool) -> list[str]:
    active = _groups(me)
    if all_groups:
        if requested:
            raise DruidError("--all-groups cannot be combined with --group")
        if not active:
            raise DruidError("no authorized DRUID groups for cross-group operation")
        return active
    if requested:
        if requested not in active and not (
            me.get("is_system_admin") and requested != "__system__"
        ):
            raise DruidError("requested group is not in your DRUID memberships")
        return [requested]
    if len(active) == 1:
        return active
    if not active:
        raise DruidError("select --group (or ask DRUID admin for membership)")
    raise DruidError(
        "multiple DRUID groups available; specify --group or "
        "--all-groups for read-only search/list"
    )


def _mcp(tool: str, args: dict, group: str) -> dict:
    result = _call(
        "POST", "/mcp", group=group,
        json_body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
        accept="application/json, text/event-stream",
    )
    if not isinstance(result, dict):
        raise DruidError("unexpected MCP response")
    if result.get("error"):
        raise DruidError("MCP returned JSON-RPC error")
    inner = result.get("result", result)
    if not isinstance(inner, dict) or inner.get("isError"):
        raise DruidError("MCP tool reported an error")
    # MCP tool return may be nested in a text content part.
    for part in inner.get("content", []):
        if isinstance(part, dict) and part.get("type") == "text":
            try:
                payload = json.loads(part["text"])
            except (KeyError, TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                return payload
    if "passages" in inner:
        return inner
    raise DruidError("MCP tool response had no documented JSON content")


def _multipart(path: Path) -> tuple[bytes, str]:
    suffix = path.suffix.lower()
    mime = MIMES.get(suffix)
    if not mime:
        raise DruidError("supported files: PDF, PNG, JPEG, TIFF, WebP")
    if not path.is_file() or path.is_symlink():
        raise DruidError("upload requires a regular, available local file")
    size = path.stat().st_size
    if size == 0 or size > MAX_UPLOAD_BYTES:
        raise DruidError("file empty or above maximum upload size")
    body = path.read_bytes()
    boundary = "druid-" + secrets.token_hex(18)
    filename = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    pre = (
        f"--{boundary}\r\nContent-Disposition: form-data; "
        f'name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    return pre + body + f"\r\n--{boundary}--\r\n".encode(), (
        "multipart/form-data; boundary=" + boundary
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--group", default="", help="DRUID internal group selection")
    p.add_argument("--all-groups", action="store_true",
                   help="run read-only search/list separately for every membership")
    p.add_argument("--confirm", action="store_true",
                   help="confirm exact deletion or reviewer resolution")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("me")
    sub.add_parser("docs")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--top-k", type=int, default=8)
    for cmd in ("document", "job", "status", "review-item", "delete"):
        cmd_p = sub.add_parser(cmd)
        cmd_p.add_argument("id")
    sub.add_parser("review-list")
    up = sub.add_parser("upload")
    up.add_argument("path")
    decision = sub.add_parser("review-resolve")
    decision.add_argument("id")
    decision.add_argument("decision", choices=("accepted", "re-routed", "rejected"))
    decision.add_argument("--note", default="")

    opts = p.parse_args(argv)
    if opts.command == "me":
        result = _identity()
    else:
        if opts.all_groups and opts.command not in ("docs", "search"):
            raise DruidError("--all-groups only supports read-only search/docs")
        if opts.command in ("delete", "review-resolve") and not opts.confirm:
            raise DruidError("destructive operation requires --confirm")
        me = _identity()
        groups = _select_group(me, opts.group, all_groups=opts.all_groups)
        outputs = []
        for group in groups:
            if opts.command == "docs":
                response = _call("GET", "/v1/documents?limit=100&offset=0",
                                 group=group)
            elif opts.command == "search":
                if not 1 <= opts.top_k <= 20:
                    raise DruidError("--top-k must be between 1 and 20")
                response = _mcp("search_knowledge_base",
                                {"query": opts.query, "top_k": opts.top_k},
                                group)
            elif opts.command == "document":
                response = _call("GET", "/v1/documents/" + _encoded(opts.id),
                                 group=group)
            elif opts.command == "status":
                response = _call("GET", "/v1/documents/" + _encoded(opts.id)
                                 + "/status", group=group)
            elif opts.command == "job":
                response = _call("GET", "/v1/jobs/" + _encoded(opts.id),
                                 group=group)
            elif opts.command == "review-list":
                response = _call("GET", "/v1/review/items?status=pending",
                                 group=group)
            elif opts.command == "review-item":
                response = _call("GET", "/v1/review/items/" + _encoded(opts.id),
                                 group=group)
            elif opts.command == "upload":
                payload, content_type = _multipart(Path(opts.path))
                response = _call("POST", "/v1/documents/upload", group=group,
                                 raw=payload, content_type=content_type)
            elif opts.command == "delete":
                response = _call("DELETE", "/v1/documents/" + _encoded(opts.id),
                                 group=group)
            elif opts.command == "review-resolve":
                response = _call(
                    "POST", "/v1/review/items/" + _encoded(opts.id) + "/resolve",
                    group=group,
                    json_body={"decision": opts.decision,
                               "detail": {"note": opts.note}},
                )
            else:
                raise DruidError("unsupported user operation")
            outputs.append({"group": group, "result": response})
        result = outputs if opts.all_groups else outputs[0]
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DruidError as exc:
        print(f"DRUID: {exc}", file=sys.stderr)
        raise SystemExit(1)
