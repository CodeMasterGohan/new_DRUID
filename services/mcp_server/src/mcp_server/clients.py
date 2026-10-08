"""Internal service client interfaces.

The MCP server never talks to Milvus, Postgres, or the queue directly. All
access goes through these abstract clients, which are implemented by the
retrieval / ingestion / catalog service owners (HTTP clients in production).
The tenant-scoped :class:`RequestContext` is always the first argument after
``self`` — implementations must enforce tenant predicates server-side and
must never accept tenant identity from any other parameter.

:class:`InMemory*` fakes are for contract tests and local development only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .context import RequestContext
from .models import PassageProvenance


# ---------------------------------------------------------------------------
# Shared value objects (produced by the owning services, consumed here)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawPassage:
    """One retrieval hit, before MCP provenance wrapping."""

    provenance: PassageProvenance
    text: str


@dataclass(frozen=True, slots=True)
class UploadTicket:
    document_id: str
    job_id: str
    upload_ticket: str  # single-use ticket for the Document API upload
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class JobStatus:
    job_id: str
    document_id: str
    state: str  # one of the §12 ingestion states
    progress_percent: int = 0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    document_id: str
    filename: str  # display name only
    status: str
    version: str
    page_count: int
    created_at: str


@dataclass(frozen=True, slots=True)
class DocumentDetail(DocumentSummary):
    artifact_refs: dict[str, str] = field(default_factory=dict)  # opaque refs


@dataclass(frozen=True, slots=True)
class ReviewItem:
    review_item_id: str
    document_id: str
    page_index: int
    page_render_ref: str  # opaque object-storage key
    native_attempt_summary: str
    visual_attempt_summary: str
    routing_diagnostics: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PageList:
    items: list[DocumentSummary]
    total: int


# ---------------------------------------------------------------------------
# Abstract clients
# ---------------------------------------------------------------------------


class RetrievalServiceClient(ABC):
    """Authorized hybrid retrieval (§10). Implementations apply the tenant
    predicate and catalog hard-filter inside their own repository layer."""

    @abstractmethod
    async def search(
        self, ctx: RequestContext, *, query: str, top_k: int
    ) -> list[RawPassage]:
        """Return up to ``top_k`` provenance-tagged passages for the tenant."""


class IngestionServiceClient(ABC):
    """Upload registration and job status (§5, §12)."""

    @abstractmethod
    async def register_upload(
        self,
        ctx: RequestContext,
        *,
        filename: str,
        mime_type: str,
        file_size_bytes: int,
        content_sha256: str,
    ) -> UploadTicket:
        """Validate quota, create the catalog record + job, return a ticket."""

    @abstractmethod
    async def get_job_status(
        self, ctx: RequestContext, *, job_id: str
    ) -> JobStatus:
        """Return the job status, resolved within the caller's tenant."""


class CatalogClient(ABC):
    """Tenant-scoped catalog reads and the review quarantine (§5, §12)."""

    @abstractmethod
    async def list_documents(
        self,
        ctx: RequestContext,
        *,
        review_required: bool | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> PageList:
        """List documents owned by the caller's tenant."""

    @abstractmethod
    async def get_document(
        self, ctx: RequestContext, *, document_id: str, include_artifacts: bool
    ) -> DocumentDetail:
        """Fetch document metadata (ownership resolved within the tenant)."""

    @abstractmethod
    async def get_review_item(
        self, ctx: RequestContext, *, review_item_id: str
    ) -> ReviewItem:
        """Fetch one quarantine item with both parse attempts + diagnostics."""

    @abstractmethod
    async def resolve_review_item(
        self,
        ctx: RequestContext,
        *,
        review_item_id: str,
        decision: str,
        note: str | None,
    ) -> str:
        """Apply accept/reroute/reject. Returns the new item state."""

    @abstractmethod
    async def delete_document(
        self, ctx: RequestContext, *, document_id: str
    ) -> str:
        """Authorize in the catalog and enqueue complete deletion (§12)."""


# ---------------------------------------------------------------------------
# In-memory fakes (tests / local dev only — never production)
# ---------------------------------------------------------------------------


class NotFoundError(Exception):
    pass


class InMemoryRetrievalClient(RetrievalServiceClient):
    def __init__(self, passages: list[RawPassage] | None = None) -> None:
        self._passages = list(passages or [])

    async def search(
        self, ctx: RequestContext, *, query: str, top_k: int
    ) -> list[RawPassage]:
        return self._passages[:top_k]


class InMemoryIngestionClient(IngestionServiceClient):
    def __init__(self) -> None:
        self.jobs: dict[str, JobStatus] = {}
        self.register_calls: list[dict] = []

    async def register_upload(
        self,
        ctx: RequestContext,
        *,
        filename: str,
        mime_type: str,
        file_size_bytes: int,
        content_sha256: str,
    ) -> UploadTicket:
        self.register_calls.append(
            {
                "group_id": ctx.group_id,  # tenant set server-side from context
                "filename": filename,
                "mime_type": mime_type,
                "file_size_bytes": file_size_bytes,
                "content_sha256": content_sha256,
            }
        )
        n = len(self.jobs) + 1
        ticket = UploadTicket(
            document_id=f"doc_{ctx.group_id}_{n}",
            job_id=f"job_{ctx.group_id}_{n}",
            upload_ticket=f"ticket_{ctx.group_id}_{n}",
            expires_in_seconds=900,
        )
        self.jobs[ticket.job_id] = JobStatus(
            job_id=ticket.job_id,
            document_id=ticket.document_id,
            state="UPLOADED",
        )
        return ticket

    async def get_job_status(
        self, ctx: RequestContext, *, job_id: str
    ) -> JobStatus:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise NotFoundError(f"unknown job: {job_id}") from None


class InMemoryCatalogClient(CatalogClient):
    def __init__(self) -> None:
        self.documents: dict[str, DocumentDetail] = {}
        self.review_items: dict[str, ReviewItem] = {}
        self.deleted: list[str] = []

    async def list_documents(
        self,
        ctx: RequestContext,
        *,
        review_required: bool | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> PageList:
        items = [d for d in self.documents.values() if d.document_id.startswith("doc_")]
        if review_required is not None:
            items = [
                d
                for d in items
                if (d.status == "REVIEW_REQUIRED") == review_required
            ]
        if status is not None:
            items = [d for d in items if d.status == status]
        return PageList(items=items[offset : offset + limit], total=len(items))

    async def get_document(
        self, ctx: RequestContext, *, document_id: str, include_artifacts: bool
    ) -> DocumentDetail:
        try:
            doc = self.documents[document_id]
        except KeyError:
            raise NotFoundError(f"unknown document: {document_id}") from None
        if not include_artifacts:
            return DocumentDetail(
                **{**doc.__dict__, "artifact_refs": {}},
            )
        return doc

    async def get_review_item(
        self, ctx: RequestContext, *, review_item_id: str
    ) -> ReviewItem:
        try:
            return self.review_items[review_item_id]
        except KeyError:
            raise NotFoundError(f"unknown review item: {review_item_id}") from None

    async def resolve_review_item(
        self,
        ctx: RequestContext,
        *,
        review_item_id: str,
        decision: str,
        note: str | None,
    ) -> str:
        if review_item_id not in self.review_items:
            raise NotFoundError(f"unknown review item: {review_item_id}")
        return {"accept": "ACCEPTED", "reroute": "RE_ROUTED", "reject": "REJECTED"}[
            decision
        ]

    async def delete_document(
        self, ctx: RequestContext, *, document_id: str
    ) -> str:
        if document_id not in self.documents:
            raise NotFoundError(f"unknown document: {document_id}")
        self.deleted.append(document_id)
        return "DELETING"
