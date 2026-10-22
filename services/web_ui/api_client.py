"""Backend client for the web_ui (spec §16: API integration).

The UI calls ONLY gateway REST endpoints with the *user's own* Bearer token.
No direct Postgres / Milvus / RustFS access. Tenant scoping is enforced
downstream by the gateway + document_api; this client never invents,
overrides, or forwards tenant identity — it forwards the token and lets
the trusted services derive identity from it.

Admin endpoints (``/v1/admin/*``) are implemented by document_api (2026-10-09;
see README.md). Calls still raise :class:`AdminNotAvailable` on 404 as a
backstop so pages can degrade gracefully against older backends.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

# JSON-RPC method shape for POST /mcp (gateway → mcp_server, Streamable HTTP).
_MCP_JSONRPC_VERSION = "2.0"


class ApiError(Exception):
    """A downstream call failed."""

    def __init__(self, status_code: int, detail: str = "") -> None:
        super().__init__(f"backend {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class AdminNotAvailable(ApiError):
    """The /v1/admin/* contract is not implemented by this backend version."""


class DruidApiClient:
    """Thin async client over the gateway. One instance per request/token."""

    def __init__(
        self,
        gateway_url: str,
        token: str,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        selected_group: str = "",
    ) -> None:
        self._base = gateway_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport
        self._selected_group = selected_group
        self._http_client: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        # The user's token is the ONLY identity signal. No tenant headers,
        # no tenant query params — the gateway derives identity from the
        # token and strips anything spoofable.
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                base_url=self._base,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    **({"X-Group-Selection": self._selected_group}
                       if self._selected_group else {}),
                },
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._http_client

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
        self._http_client = None

    async def __aenter__(self) -> "DruidApiClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        client = self._client()
        try:
            try:
                resp = await client.request(method, path, **kwargs)
            except RuntimeError:
                self._http_client = None
                client = self._client()
                resp = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            log.warning("backend call failed: %s %s: %s", method, path, exc)
            raise ApiError(502, "backend unreachable") from exc
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = str(resp.json().get("detail", ""))
            except Exception:
                detail = resp.text[:200]
            raise ApiError(resp.status_code, detail)
        if not resp.content:
            return None
        try:
            return resp.json()
        except Exception:
            return resp.text

    async def identity(self) -> dict:
        return await self._request("GET", "/v1/identity/me")

    async def admin_list_users(self) -> dict:
        return await self._request("GET", "/v1/admin/users")

    async def admin_create_user(self, data: dict) -> dict:
        return await self._request("POST", "/v1/admin/users", json=data)

    async def admin_update_user(self, subject: str, data: dict) -> dict:
        return await self._request("PATCH", f"/v1/admin/users/{subject}", json=data)

    async def admin_list_groups(self) -> dict:
        return await self._request("GET", "/v1/admin/groups")

    async def admin_create_group(self, name: str) -> dict:
        return await self._request("POST", "/v1/admin/groups", json={"name": name})

    async def admin_set_member(self, group: str, subject: str, role: str) -> dict:
        from urllib.parse import quote
        return await self._request(
            "PUT", f"/v1/admin/groups/{quote(group)}/members/{quote(subject, safe='')}",
            json={"subject": subject, "role": role},
        )

    async def admin_remove_member(self, group: str, subject: str) -> dict:
        from urllib.parse import quote
        return await self._request(
            "DELETE", f"/v1/admin/groups/{quote(group)}/members/{quote(subject, safe='')}"
        )

    # -- documents --------------------------------------------------------

    async def list_documents(
        self,
        review_required: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if review_required is not None:
            params["review_required"] = str(review_required).lower()
        return await self._request("GET", "/v1/documents", params=params)

    async def get_document(self, document_id: str) -> dict:
        return await self._request("GET", f"/v1/documents/{document_id}")

    async def get_document_status(self, document_id: str) -> dict:
        return await self._request("GET", f"/v1/documents/{document_id}/status")

    async def delete_document(self, document_id: str) -> dict:
        return await self._request("DELETE", f"/v1/documents/{document_id}")

    async def upload_document(
        self, filename: str, content: bytes, content_type: str
    ) -> dict:
        files = {"file": (filename, content, content_type)}
        return await self._request("POST", "/v1/documents/upload", files=files)

    # -- review -----------------------------------------------------------

    async def list_review_items(
        self, status: str = "pending", limit: int = 100, offset: int = 0
    ) -> dict:
        return await self._request(
            "GET",
            "/v1/review/items",
            params={"status": status, "limit": limit, "offset": offset},
        )

    async def get_review_item(self, item_id: str) -> dict:
        return await self._request("GET", f"/v1/review/items/{item_id}")

    async def resolve_review_item(
        self, item_id: str, decision: str, note: str | None = None
    ) -> dict:
        # document_api's ResolveReviewRequest.detail is a JSON object; a bare
        # string is rejected with 422. Same shape the MCP server sends.
        body: dict[str, Any] = {
            "decision": decision,
            "detail": {"note": note} if note else {},
        }
        return await self._request(
            "POST", f"/v1/review/items/{item_id}/resolve", json=body
        )

    # -- search (via gateway /mcp → search_knowledge_base) -----------------

    async def search(self, query: str, top_k: int = 8) -> list[dict]:
        payload = {
            "jsonrpc": _MCP_JSONRPC_VERSION,
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_knowledge_base",
                "arguments": {"query": query, "top_k": top_k},
            },
        }
        data = await self._request("POST", "/mcp", json=payload)
        try:
            text = data["result"]["content"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ApiError(502, "unexpected MCP search response shape") from exc
        import json as _json

        try:
            parsed = _json.loads(text)
        except Exception as exc:
            raise ApiError(502, "search response was not JSON") from exc
        return parsed.get("passages", [])

    # -- admin (document_api /v1/admin/*; admin role required) --------------

    async def _admin(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            return await self._request(method, path, **kwargs)
        except ApiError as exc:
            if exc.status_code == 404:
                raise AdminNotAvailable(
                    404,
                    f"{path} is not implemented by document_api yet "
                    "(see web_ui README.md: admin contract)",
                ) from exc
            raise

    async def admin_list_documents(
        self, tenant: str | None = None, limit: int = 100, offset: int = 0
    ) -> dict:
        """Cross-tenant document table. ``tenant`` scopes to one group_id;
        omitted means all tenants. Identity still comes from the token."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if tenant:
            params["tenant"] = tenant
        return await self._admin("GET", "/v1/admin/documents", params=params)

    async def admin_list_jobs(
        self, tenant: str | None = None, limit: int = 100, offset: int = 0
    ) -> dict:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if tenant:
            params["tenant"] = tenant
        return await self._admin("GET", "/v1/admin/jobs", params=params)

    async def admin_get_quotas(self) -> dict:
        return await self._admin("GET", "/v1/admin/quotas")


__all__ = ["AdminNotAvailable", "ApiError", "DruidApiClient"]
