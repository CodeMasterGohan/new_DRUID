"""Production HTTP implementations of the service client ABCs (spec §11).

The MCP server never touches Milvus, Postgres, or the queue directly. These
clients call the owning services over HTTP, propagating the caller's tenant
identity as HMAC-signed internal headers (spec §4) — the identity always
comes from the validated :class:`RequestContext`, never from any other
parameter.

Two signing schemes are in play (they are NOT interchangeable):

* document_api (documents, review, delete): the ``druid-v1`` scheme —
  ``X-Druid-User-Id`` / ``X-Druid-Group-Id`` / ``X-Druid-Roles`` (sorted CSV)
  / ``X-Druid-Request-Id`` / ``X-Druid-Internal-Sig`` (``v1:<hex>``).
  Mirrors ``services/document_api/internal_auth.py``.
* retrieval (``POST /search``): the timestamped scheme — ``X-Druid-User`` /
  ``X-Druid-Group`` / ``X-Druid-Roles`` / ``X-Druid-Request-Id`` /
  ``X-Druid-Timestamp`` / ``X-Druid-Signature`` (bare hex, 300s TTL).
  Mirrors ``services/retrieval/main.py``.

Catalog access goes through document_api's existing endpoints — there is no
direct catalog HTTP API, and this module deliberately does not invent one.

Documented gaps (not papered over):

* ``list_documents`` — document_api's list response carries no ``total``
  count and no per-document version status, so ``total`` is reported as
  ``offset + len(page)`` (exact when the page is short, otherwise a lower
  bound), and each listed document is enriched with one detail call to
  populate ``status`` / ``version`` / ``page_count``. If that N+1 pattern
  gets too chatty, add version status to document_api's list response.
* ``POST /search`` supports an optional ``top_k`` parameter and slices
  client-side as well.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time

import httpx
from pydantic import ValidationError

from .clients import (
    CatalogClient,
    DocumentDetail,
    DocumentSummary,
    IngestionServiceClient,
    JobStatus,
    NotFoundError,
    PageList,
    RawPassage,
    RetrievalServiceClient,
    ReviewItem,
    UploadTicket,
)
from .context import RequestContext
from .models import PassageProvenance

log = logging.getLogger(__name__)

#: Max concurrent detail fetches when enriching a list page (§ "Documented
#: gaps" above). Bounded so a 100-item page cannot fan out unboundedly.
_LIST_ENRICH_CONCURRENCY = 8

#: Length cap for review-attempt summaries surfaced through the MCP tool.
_SUMMARY_CHARS = 500

#: MCP decision -> document_api ReviewDecision.
_REVIEW_DECISIONS = {
    "accept": "accepted",
    "reroute": "re-routed",
    "reject": "rejected",
}


class ServiceClientError(Exception):
    """A downstream service call failed.

    Covers connection/timeout failures, auth rejections, unexpected HTTP
    statuses, and response payloads that do not match the service contract.
    The ABC defines :class:`NotFoundError` for 404s; everything else maps
    here — no new exception names are introduced per case.
    """


# ---------------------------------------------------------------------------
# Internal identity headers (spec §4)
# ---------------------------------------------------------------------------


def sign_document_api_headers(
    ctx: RequestContext, secret: str
) -> dict[str, str]:
    """Build the ``druid-v1`` signed headers document_api expects.

    Byte-exact mirror of ``services/document_api/internal_auth.py``:
    canonical ``"druid-v1|{user}|{group}|{roles_csv}|{request_id}"`` with the
    roles comma-joined *sorted*, signature ``"v1:<hex-hmac-sha256>"``.
    """
    roles_csv = ",".join(sorted(ctx.roles))
    canonical = (
        f"druid-v1|{ctx.user_id}|{ctx.group_id}|{roles_csv}|{ctx.request_id}"
    )
    digest = hmac.new(
        secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "X-Druid-User-Id": ctx.user_id,
        "X-Druid-Group-Id": ctx.group_id,
        "X-Druid-Roles": roles_csv,
        "X-Druid-Request-Id": ctx.request_id,
        "X-Druid-Internal-Sig": f"v1:{digest}",
    }


def sign_retrieval_headers(ctx: RequestContext, secret: str) -> dict[str, str]:
    """Build the timestamped signed headers the retrieval service expects.

    Byte-exact mirror of ``services/retrieval/main.py``: payload
    ``"{user}|{group}|{roles}|{request_id}|{timestamp}"`` with a bare hex
    HMAC-SHA256 signature. The 300s TTL is enforced server-side.
    """
    roles_csv = ",".join(sorted(ctx.roles))
    timestamp = str(time.time())
    payload = (
        f"{ctx.user_id}|{ctx.group_id}|{roles_csv}|{ctx.request_id}|{timestamp}"
    )
    signature = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "X-Druid-User": ctx.user_id,
        "X-Druid-Group": ctx.group_id,
        "X-Druid-Roles": roles_csv,
        "X-Druid-Request-Id": ctx.request_id,
        "X-Druid-Timestamp": timestamp,
        "X-Druid-Signature": signature,
    }


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class _BaseHttpClient:
    """Shared httpx plumbing: timeouts, error mapping, JSON parsing."""

    def __init__(
        self,
        base_url: str,
        internal_secret: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("base_url is required for HTTP service clients")
        self._base_url = base_url.rstrip("/")
        self._secret = internal_secret
        self._timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def _require_secret(self, operation: str) -> str:
        if not self._secret:
            raise ServiceClientError(
                f"{operation}: INTERNAL_SECRET is not configured; cannot sign "
                "downstream identity headers (fail closed)"
            )
        return self._secret

    async def _call(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        operation: str,
        **kwargs,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        client = self._get_client()
        try:
            try:
                response = await client.request(
                    method, url, headers=headers, **kwargs
                )
            except RuntimeError:
                # Recreate client if the event loop changed across calls.
                self._client = None
                client = self._get_client()
                response = await client.request(
                    method, url, headers=headers, **kwargs
                )
        except httpx.HTTPError as exc:
            raise ServiceClientError(
                f"{operation}: downstream request failed: {exc}"
            ) from exc
        return response

    @staticmethod
    def _json(response: httpx.Response, operation: str) -> dict:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ServiceClientError(
                f"{operation}: downstream returned non-JSON "
                f"(status {response.status_code}): {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ServiceClientError(
                f"{operation}: downstream returned a non-object payload "
                f"(status {response.status_code})"
            )
        return payload

    @staticmethod
    def _raise_for_status(
        response: httpx.Response,
        operation: str,
        *,
        ok: tuple[int, ...],
        not_found_detail: str,
    ) -> None:
        if response.status_code in ok:
            return
        if response.status_code == 404:
            raise NotFoundError(not_found_detail)
        raise ServiceClientError(
            f"{operation}: downstream returned HTTP {response.status_code}"
        )


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


class HttpRetrievalClient(RetrievalServiceClient, _BaseHttpClient):
    """Authorized hybrid retrieval over HTTP (``POST {base_url}/search``)."""

    async def search(
        self, ctx: RequestContext, *, query: str, top_k: int
    ) -> list[RawPassage]:
        operation = "retrieval search"
        secret = self._require_secret(operation)
        body: dict[str, object] = {"query": query}
        if top_k > 8:
            body["top_k"] = top_k
        response = await self._call(
            "POST",
            "/search",
            sign_retrieval_headers(ctx, secret),
            operation,
            json=body,
        )
        self._raise_for_status(
            response,
            operation,
            ok=(200,),
            not_found_detail="retrieval search endpoint not found",
        )
        payload = self._json(response, operation)
        results = payload.get("results")
        if not isinstance(results, list):
            raise ServiceClientError(
                f"{operation}: response has no 'results' list"
            )
        passages: list[RawPassage] = []
        for hit in results[:top_k]:
            passages.append(self._to_passage(hit, operation))
        return passages

    @staticmethod
    def _to_passage(hit: object, operation: str) -> RawPassage:
        if not isinstance(hit, dict):
            raise ServiceClientError(
                f"{operation}: result entry is not an object"
            )
        try:
            provenance = PassageProvenance(
                document_id=hit["document_id"],
                filename=hit["filename"],
                chunk_id=hit["chunk_id"],
                page_start=hit["page_start"],
                page_end=hit["page_end"],
                modality=hit["modality"],
                parser=hit["provenance"],
                source_reference=hit["source_reference"],
                page_image_ref=hit.get("page_image_ref"),
            )
        except (KeyError, TypeError, ValidationError) as exc:
            raise ServiceClientError(
                f"{operation}: result entry failed provenance validation: {exc}"
            ) from exc
        text = hit.get("text", "")
        if not isinstance(text, str):
            raise ServiceClientError(
                f"{operation}: result entry has non-string text"
            )
        return RawPassage(provenance=provenance, text=text)


# ---------------------------------------------------------------------------
# Catalog (via document_api)
# ---------------------------------------------------------------------------


def _as_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[no-untyped-call]
    return str(value)


def _attempt_summary(attempts: dict, name: str) -> str:
    """Condense one parse attempt into a short human-readable summary."""
    attempt = attempts.get(name)
    if attempt is None:
        return f"no {name} attempt recorded"
    if isinstance(attempt, str):
        return attempt[:_SUMMARY_CHARS]
    if not isinstance(attempt, dict):
        return str(attempt)[:_SUMMARY_CHARS]
    error = attempt.get("error")
    if error:
        return f"error: {error}"[:_SUMMARY_CHARS]
    for key in ("markdown", "text", "content"):
        content = attempt.get(key)
        if content:
            return f"{name} attempt excerpt: {content}"[:_SUMMARY_CHARS]
    return f"{name} attempt (keys: {', '.join(sorted(attempt))})"


class HttpCatalogClient(CatalogClient, _BaseHttpClient):
    """Tenant-scoped catalog reads via document_api's existing endpoints.

    There is no direct catalog HTTP API; every method below maps to a
    document_api route (see module docstring for the documented gaps).
    """

    # -- list -----------------------------------------------------------

    async def list_documents(
        self,
        ctx: RequestContext,
        *,
        review_required: bool | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> PageList:
        operation = "catalog list_documents"
        secret = self._require_secret(operation)

        # document_api's list supports review_required server-side but no
        # status filter; other statuses filter client-side after enrichment.
        client_status: str | None = None
        if status is not None and status != "REVIEW_REQUIRED":
            server_review_required = review_required
            client_status = status
        else:
            server_review_required = (
                review_required
                if review_required is not None
                else (True if status == "REVIEW_REQUIRED" else None)
            )

        params: dict[str, str] = {
            "limit": str(limit),
            "offset": str(offset),
        }
        if server_review_required is not None:
            params["review_required"] = (
                "true" if server_review_required else "false"
            )
        response = await self._call(
            "GET",
            "/v1/documents",
            sign_document_api_headers(ctx, secret),
            operation,
            params=params,
        )
        self._raise_for_status(
            response,
            operation,
            ok=(200,),
            not_found_detail="document list endpoint not found",
        )
        payload = self._json(response, operation)
        docs = payload.get("documents")
        if not isinstance(docs, list):
            raise ServiceClientError(
                f"{operation}: response has no 'documents' list"
            )

        # The list response carries no version status, so each document is
        # enriched with one detail call (bounded concurrency).
        semaphore = asyncio.Semaphore(_LIST_ENRICH_CONCURRENCY)

        async def _enrich(doc: dict) -> DocumentSummary:
            async with semaphore:
                return await self._summary_from_detail(ctx, secret, doc)

        items = await asyncio.gather(
            *(_enrich(d) for d in docs if isinstance(d, dict))
        )
        if client_status is not None:
            items = [i for i in items if i.status == client_status]
        # document_api reports no total: exact when the page is short,
        # otherwise a lower bound.
        return PageList(items=list(items), total=offset + len(docs))

    async def _summary_from_detail(
        self, ctx: RequestContext, secret: str, doc: dict
    ) -> DocumentSummary:
        document_id = doc.get("document_id")
        if not document_id:
            raise ServiceClientError(
                "catalog list_documents: list entry has no document_id"
            )
        detail = await self._fetch_detail(ctx, secret, str(document_id))
        return DocumentSummary(
            document_id=detail.document_id,
            filename=detail.filename,
            status=detail.status,
            version=detail.version,
            page_count=detail.page_count,
            created_at=detail.created_at,
        )

    # -- get ------------------------------------------------------------

    async def get_document(
        self, ctx: RequestContext, *, document_id: str, include_artifacts: bool
    ) -> DocumentDetail:
        operation = "catalog get_document"
        secret = self._require_secret(operation)
        detail = await self._fetch_detail(ctx, secret, document_id)
        if not include_artifacts:
            return DocumentDetail(
                document_id=detail.document_id,
                filename=detail.filename,
                status=detail.status,
                version=detail.version,
                page_count=detail.page_count,
                created_at=detail.created_at,
                artifact_refs={},
            )
        return detail

    async def _fetch_detail(
        self, ctx: RequestContext, secret: str, document_id: str
    ) -> DocumentDetail:
        operation = "catalog get_document"
        response = await self._call(
            "GET",
            f"/v1/documents/{document_id}",
            sign_document_api_headers(ctx, secret),
            operation,
        )
        self._raise_for_status(
            response,
            operation,
            ok=(200,),
            not_found_detail=f"unknown document: {document_id}",
        )
        payload = self._json(response, operation)
        versions = payload.get("versions") or []
        if not isinstance(versions, list):
            raise ServiceClientError(
                f"{operation}: response has malformed 'versions'"
            )
        # Mirror the server: active version, else the latest listed.
        target = next(
            (v for v in versions if isinstance(v, dict) and v.get("is_active")),
            versions[0] if versions else None,
        )
        artifacts: dict[str, str] = {}
        for name, ref in (payload.get("artifacts") or {}).items():
            # Opaque object-storage keys only — never presigned URLs.
            if isinstance(ref, dict) and ref.get("key"):
                artifacts[str(name)] = str(ref["key"])
        return DocumentDetail(
            document_id=str(payload.get("document_id", document_id)),
            filename=str(payload.get("display_name", "")),
            status=str(target.get("status", "UPLOADED"))
            if isinstance(target, dict)
            else "UPLOADED",
            version=str(target.get("version_number", 0))
            if isinstance(target, dict)
            else "0",
            page_count=int(target.get("page_count") or 0)
            if isinstance(target, dict)
            else 0,
            created_at=_as_str(payload.get("created_at")),
            artifact_refs=artifacts,
        )

    # -- review ---------------------------------------------------------

    async def get_review_item(
        self, ctx: RequestContext, *, review_item_id: str
    ) -> ReviewItem:
        operation = "catalog get_review_item"
        secret = self._require_secret(operation)
        response = await self._call(
            "GET",
            f"/v1/review/items/{review_item_id}",
            sign_document_api_headers(ctx, secret),
            operation,
        )
        self._raise_for_status(
            response,
            operation,
            ok=(200,),
            not_found_detail=f"unknown review item: {review_item_id}",
        )
        payload = self._json(response, operation)
        attempts = payload.get("parse_attempts") or {}
        diagnostics = payload.get("routing_diagnostics") or {}
        if not isinstance(attempts, dict) or not isinstance(diagnostics, dict):
            raise ServiceClientError(
                f"{operation}: response has malformed attempt/diagnostic fields"
            )
        try:
            return ReviewItem(
                review_item_id=str(payload["item_id"]),
                document_id=str(payload["document_id"]),
                page_index=int(payload["page_index"]),
                # The opaque object-storage key — never the presigned URL.
                page_render_ref=str(payload.get("page_render_key", "")),
                native_attempt_summary=_attempt_summary(attempts, "native"),
                visual_attempt_summary=_attempt_summary(attempts, "visual"),
                routing_diagnostics={
                    str(k): str(v) for k, v in diagnostics.items()
                },
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceClientError(
                f"{operation}: malformed review item payload: {exc}"
            ) from exc

    async def resolve_review_item(
        self,
        ctx: RequestContext,
        *,
        review_item_id: str,
        decision: str,
        note: str | None,
    ) -> str:
        operation = "catalog resolve_review_item"
        secret = self._require_secret(operation)
        try:
            api_decision = _REVIEW_DECISIONS[decision]
        except KeyError:
            raise ValueError(
                f"unknown review decision: {decision!r}; "
                f"expected one of: {', '.join(sorted(_REVIEW_DECISIONS))}"
            ) from None
        body: dict[str, object] = {
            "decision": api_decision,
            "detail": {"note": note} if note else {},
        }
        response = await self._call(
            "POST",
            f"/v1/review/items/{review_item_id}/resolve",
            sign_document_api_headers(ctx, secret),
            operation,
            json=body,
        )
        self._raise_for_status(
            response,
            operation,
            ok=(200,),
            not_found_detail=f"unknown review item: {review_item_id}",
        )
        payload = self._json(response, operation)
        status = payload.get("status")
        if not status:
            raise ServiceClientError(
                f"{operation}: resolve response has no 'status'"
            )
        return str(status)

    # -- delete ---------------------------------------------------------

    async def delete_document(
        self, ctx: RequestContext, *, document_id: str
    ) -> str:
        operation = "catalog delete_document"
        secret = self._require_secret(operation)
        response = await self._call(
            "DELETE",
            f"/v1/documents/{document_id}",
            sign_document_api_headers(ctx, secret),
            operation,
        )
        self._raise_for_status(
            response,
            operation,
            ok=(200, 202),
            not_found_detail=f"unknown document: {document_id}",
        )
        payload = self._json(response, operation)
        status = payload.get("status")
        if not status:
            raise ServiceClientError(
                f"{operation}: delete response has no 'status'"
            )
        return str(status)


# ---------------------------------------------------------------------------
# Ingestion (via document_api)
# ---------------------------------------------------------------------------


class HttpIngestionClient(IngestionServiceClient, _BaseHttpClient):
    """Upload registration and job status via document_api.

    Two-phase upload (spec §5): :meth:`register_upload` declares the file
    and receives a single-use upload ticket
    (``POST /v1/documents/register``); the tool caller uploads the bytes
    with that ticket (``POST /v1/documents/{id}/bytes``) and polls
    :meth:`get_job_status` (``GET /v1/jobs/{job_id}``). This client never
    holds the file bytes.
    """

    async def register_upload(
        self,
        ctx: RequestContext,
        *,
        filename: str,
        mime_type: str,
        file_size_bytes: int,
        content_sha256: str,
    ) -> UploadTicket:
        operation = "ingestion register_upload"
        secret = self._require_secret(operation)
        response = await self._call(
            "POST",
            "/v1/documents/register",
            sign_document_api_headers(ctx, secret),
            operation,
            json={
                "filename": filename,
                "mime_type": mime_type,
                "file_size_bytes": file_size_bytes,
                "content_sha256": content_sha256,
            },
        )
        self._raise_for_status(
            response,
            operation,
            ok=(200, 201),
            not_found_detail="upload registration endpoint not found",
        )
        payload = self._json(response, operation)
        try:
            return UploadTicket(
                document_id=str(payload["document_id"]),
                job_id=str(payload["job_id"]),
                upload_ticket=str(payload["upload_ticket"]),
                expires_in_seconds=int(payload["expires_in_seconds"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceClientError(
                f"{operation}: malformed registration payload: {exc}"
            ) from exc

    async def get_job_status(
        self, ctx: RequestContext, *, job_id: str
    ) -> JobStatus:
        operation = "ingestion get_job_status"
        secret = self._require_secret(operation)
        response = await self._call(
            "GET",
            f"/v1/jobs/{job_id}",
            sign_document_api_headers(ctx, secret),
            operation,
        )
        self._raise_for_status(
            response,
            operation,
            ok=(200,),
            not_found_detail=f"unknown job: {job_id}",
        )
        payload = self._json(response, operation)
        try:
            state = str(payload["state"])
        except KeyError as exc:
            raise ServiceClientError(
                f"{operation}: status payload has no 'state': {exc}"
            ) from exc
        # The API reports no progress percentage; READY is the only
        # terminal success state, so progress reads 100 there, 0 otherwise.
        return JobStatus(
            job_id=str(payload.get("job_id", job_id)),
            document_id=str(payload.get("document_id") or ""),
            state=state,
            progress_percent=100 if state == "READY" else 0,
            detail=str(payload.get("error") or ""),
        )


__all__ = [
    "HttpCatalogClient",
    "HttpIngestionClient",
    "HttpRetrievalClient",
    "ServiceClientError",
    "sign_document_api_headers",
    "sign_retrieval_headers",
]
