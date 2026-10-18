#!/usr/bin/env python3
"""DRUID REST administration: zero dependencies, no shelling out or token echo.

API origin and Keycloak access token come from environment variables.
Destructive methods require --confirm. All operations go through the
authenticated gateway; identity, group and roles are verified server-side.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def _url_path(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def request(method: str, path: str, data: dict | None = None,
            group: str = "") -> dict:
    origin = os.environ.get("DRUID_API_BASE", "").rstrip("/")
    token = os.environ.get("DRUID_ACCESS_TOKEN", "")
    if not origin or not token:
        raise ValueError("DRUID_API_BASE and DRUID_ACCESS_TOKEN are required")
    allowed_local = (os.environ.get("DRUID_ALLOW_LOCAL_HTTP") == "1"
                     and origin in ("http://127.0.0.1:8000", "http://localhost:8000"))
    if not origin.startswith("https://") and not allowed_local:
        raise ValueError("refusing to send bearer token over non-HTTPS origin")
    if not path.startswith("/v1/") and path != "/mcp":
        raise ValueError("only documented DRUID gateway API paths are allowed")
    headers = {"Authorization": "Bearer " + token,
               "Accept": "application/json"}
    if group:
        headers["X-Group-Selection"] = group
    payload = None
    if data is not None:
        payload = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(origin + path, data=payload, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", "replace")
        raise RuntimeError(f"DRUID API HTTP {exc.code}: {detail}") from None
    return json.loads(body.decode("utf-8")) if body else {"ok": True}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--group", default="", help="select an authorized DRUID group")
    p.add_argument("--confirm", action="store_true", help="approve destructive mutation")
    sub = p.add_subparsers(dest="command", required=True)
    for command in ("me", "groups", "users", "admin-documents", "jobs", "quotas", "docs"):
        sub.add_parser(command)
    group = sub.add_parser("create-group")
    group.add_argument("name")
    user = sub.add_parser("create-user")
    user.add_argument("subject")
    user.add_argument("--username", default="")
    user.add_argument("--email", default="")
    member = sub.add_parser("set-member")
    member.add_argument("group")
    member.add_argument("subject")
    member.add_argument("role", choices=("member", "manager"))
    remove = sub.add_parser("remove-member")
    remove.add_argument("group")
    remove.add_argument("subject")
    drop = sub.add_parser("delete-document")
    drop.add_argument("document_id")
    deactivate = sub.add_parser("disable-user")
    deactivate.add_argument("subject")
    dg = sub.add_parser("disable-group")
    dg.add_argument("name")
    generic = sub.add_parser("api")
    generic.add_argument("method", choices=("GET", "POST", "PUT", "PATCH", "DELETE"))
    generic.add_argument("path")
    generic.add_argument("--json", default="", help="JSON object for request body")
    args = p.parse_args(argv)
    routes = {
        "me": ("GET", "/v1/identity/me", None),
        "groups": ("GET", "/v1/admin/groups", None),
        "users": ("GET", "/v1/admin/users", None),
        "admin-documents": ("GET", "/v1/admin/documents", None),
        "jobs": ("GET", "/v1/admin/jobs", None),
        "quotas": ("GET", "/v1/admin/quotas", None),
        "docs": ("GET", "/v1/documents", None),
    }
    if args.command in routes:
        method, path, body = routes[args.command]
    elif args.command == "create-group":
        method, path, body = ("POST", "/v1/admin/groups", {"name": args.name})
    elif args.command == "create-user":
        method, path, body = ("POST", "/v1/admin/users", {
            "subject": args.subject, "username": args.username,
            "email": args.email or None,
        })
    elif args.command == "set-member":
        method, path, body = (
            "PUT", f"/v1/admin/groups/{_url_path(args.group)}/members/{_url_path(args.subject)}",
            {"subject": args.subject, "role": args.role},
        )
    elif args.command == "remove-member":
        method, path, body = (
            "DELETE", f"/v1/admin/groups/{_url_path(args.group)}/members/{_url_path(args.subject)}", None,
        )
    elif args.command == "delete-document":
        method, path, body = "DELETE", f"/v1/documents/{_url_path(args.document_id)}", None
    elif args.command == "disable-user":
        method, path, body = "PATCH", f"/v1/admin/users/{_url_path(args.subject)}", {"is_active": False}
    elif args.command == "disable-group":
        method, path, body = "PATCH", f"/v1/admin/groups/{_url_path(args.name)}", {"is_active": False}
    else:
        method, path = args.method, args.path
        body = json.loads(args.json) if args.json else None
        if body is not None and not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
    destructive = (method == "DELETE" or args.command.startswith("disable-")
                   or method == "PATCH" and bool(body) and any(
                       name in body for name in ("is_active", "is_system_admin")))
    if destructive and not args.confirm:
        raise ValueError("destructive operation requires --confirm")
    result = request(method, path, body, args.group)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
