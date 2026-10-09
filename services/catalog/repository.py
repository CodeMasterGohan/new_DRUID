"""Tenant-scoped catalog repository (spec §4).

This is the application-layer authorization boundary for catalog data. Every
read and write is scoped to the trusted request context's ``group_id``:

* Search / query / list — mandatory tenant predicate.
* Fetch by ID — ownership resolved within the authenticated tenant.
* Insert — ``tenant_id`` is set from the trusted context; any
  caller-supplied tenant value is ignored.
* Delete — authorized in the catalog (soft-delete here; physical deletion of
  Milvus chunks, objects, and caches is the deletion job's responsibility).

The searchability hard filter (§10 step 3, §12) lives here too: only
versions with ``is_active=True`` AND ``status=READY`` on non-deleted
documents are ever returned as searchable. This is enforced in the
application layer and never relies on Milvus index/compaction state.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.common.request_context import RequestContext

from .errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    TenantContextError,
)
from .models import (
    Document,
    DocumentACL,
    DocumentVersion,
    IngestionState,
    Job,
    ReviewQuarantineItem,
    Tenant,
)

log = logging.getLogger(__name__)

# Statuses that may never appear in search results, even if some other flag
# were wrong. READY is the only searchable state; the query below also
# requires is_active=True, so this set is defense-in-depth documentation.
_UNSEARCHABLE_STATES = frozenset(
    {
        IngestionState.UPLOADED,
        IngestionState.QUEUED,
        IngestionState.PARSING,
        IngestionState.CHUNKING,
        IngestionState.EMBEDDING,
        IngestionState.INDEXING,
        IngestionState.RETRY_PENDING,
        IngestionState.FAILED,
        IngestionState.REVIEW_REQUIRED,
        IngestionState.DELETING,
        IngestionState.DELETED,
    }
)


class CatalogRepository:
    """All catalog access for one trusted request context."""

    def __init__(self, session: Session, context: RequestContext) -> None:
        if context is None or not context.user_id or not context.group_id:
            # Fail closed: no context, no data. Never default to a tenant.
            raise TenantContextError(
                "catalog access requires a trusted request context with "
                "user_id and group_id"
            )
        self.session = session
        self.context = context

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _tenant_id(self) -> uuid.UUID:
        """Resolve the context's group_id to the tenant row.

        The context carries the tenant's *name* (the ``group_id`` claim); the
        UUID is resolved here so a forged claim string alone is useless
        without a matching active tenant row.
        """
        tenant = (
            self.session.execute(
                select(Tenant).where(
                    Tenant.name == self.context.group_id,
                    Tenant.is_active.is_(True),
                )
            )
            .scalars()
            .one_or_none()
        )
        if tenant is None:
            raise AuthorizationError(
                f"tenant {self.context.group_id!r} is unknown or inactive"
            )
        return tenant.id

    # ------------------------------------------------------------------
    # documents
    # ------------------------------------------------------------------
    def create_document(
        self,
        *,
        display_name: str,
        content_checksum: str,
        tenant_id: uuid.UUID | str | None = None,
        **kwargs,
    ) -> Document:
        """Create a document owned by the context's tenant.

        ``tenant_id`` is accepted as a keyword only so callers *cannot*
        smuggle one in: any caller-supplied value is ignored and the trusted
        context's tenant is used (§4 "safe writes").
        """
        doc = Document(
            tenant_id=self._tenant_id(),
            display_name=display_name,
            content_checksum=content_checksum,
            **kwargs,
        )
        self.session.add(doc)
        self.session.flush()
        return doc

    def get_document(self, document_id: uuid.UUID) -> Document:
        """Fetch a document by ID, resolved within the authenticated tenant."""
        doc = (
            self.session.execute(
                select(Document).where(
                    Document.id == document_id,
                    Document.tenant_id == self._tenant_id(),
                    Document.is_deleted.is_(False),
                )
            )
            .scalars()
            .one_or_none()
        )
        if doc is None:
            raise NotFoundError(f"document {document_id} not found")
        return doc

    def list_documents(
        self,
        *,
        review_required: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Document]:
        """List the tenant's documents.

        ``review_required=True`` filters to documents with at least one
        pending quarantine item (§5 review queue surface).
        """
        stmt = (
            select(Document)
            .where(
                Document.tenant_id == self._tenant_id(),
                Document.is_deleted.is_(False),
            )
            .order_by(Document.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if review_required is not None:
            subq = (
                select(ReviewQuarantineItem.document_id)
                .where(
                    ReviewQuarantineItem.tenant_id == self._tenant_id(),
                    ReviewQuarantineItem.status == "pending",
                )
                .distinct()
            )
            if review_required:
                stmt = stmt.where(Document.id.in_(subq))
            else:
                stmt = stmt.where(~Document.id.in_(subq))
        return list(self.session.execute(stmt).scalars().all())

    def soft_delete_document(self, document_id: uuid.UUID) -> Document:
        """Authorize and mark a document deleted in the catalog.

        Physical deletion (Milvus chunks, objects, caches) is performed by
        the deletion job; this only removes catalog visibility (§12).
        """
        doc = self.get_document(document_id)
        doc.is_deleted = True
        doc.deleted_at = dt.datetime.now(dt.timezone.utc)
        self.session.flush()
        log.info(
            "document soft-deleted: document=%s group=%s request_id=%s",
            document_id,
            self.context.group_id,
            self.context.request_id,
        )
        return doc

    # ------------------------------------------------------------------
    # versions
    # ------------------------------------------------------------------
    def create_version(
        self,
        document_id: uuid.UUID,
        *,
        parser: str,
        parser_revision: str,
        page_count: int | None = None,
    ) -> DocumentVersion:
        doc = self.get_document(document_id)  # ownership check
        existing = (
            self.session.execute(
                select(DocumentVersion.version_number)
                .where(DocumentVersion.document_id == doc.id)
                .order_by(DocumentVersion.version_number.desc())
                .limit(1)
            ).scalar()
            or 0
        )
        version = DocumentVersion(
            document_id=doc.id,
            tenant_id=doc.tenant_id,
            version_number=existing + 1,
            status=IngestionState.UPLOADED,
            parser=parser,
            parser_revision=parser_revision,
            page_count=page_count,
        )
        self.session.add(version)
        self.session.flush()
        return version

    def get_version(self, version_id: uuid.UUID) -> DocumentVersion:
        version = (
            self.session.execute(
                select(DocumentVersion).where(
                    DocumentVersion.id == version_id,
                    DocumentVersion.tenant_id == self._tenant_id(),
                )
            )
            .scalars()
            .one_or_none()
        )
        if version is None:
            raise NotFoundError(f"document version {version_id} not found")
        return version

    def get_active_version(self, document_id: uuid.UUID) -> DocumentVersion | None:
        doc = self.get_document(document_id)
        if doc.active_version_id is None:
            return None
        return self.get_version(doc.active_version_id)

    def list_versions(self, document_id: uuid.UUID) -> list[DocumentVersion]:
        doc = self.get_document(document_id)
        stmt = (
            select(DocumentVersion)
            .where(DocumentVersion.document_id == doc.id)
            .order_by(DocumentVersion.version_number.desc())
        )
        return list(self.session.execute(stmt).scalars().all())

    def update_version_status(
        self, version_id: uuid.UUID, status: IngestionState, **fields
    ) -> DocumentVersion:
        version = self.get_version(version_id)
        version.status = status
        for key, value in fields.items():
            if not hasattr(version, key):
                raise ValueError(f"unknown DocumentVersion field: {key}")
            setattr(version, key, value)
        self.session.flush()
        return version

    # ------------------------------------------------------------------
    # searchability hard filter (§10 step 3, §12 "no partial visibility")
    # ------------------------------------------------------------------
    def searchable_versions_query(self):
        """Query limited to versions that may appear in search results.

        A version is searchable only when ALL of these hold:

        * it belongs to the context's tenant,
        * ``is_active`` is true (exactly one active version per document),
        * ``status`` is ``READY`` (never UPLOADED/INDEXING/FAILED/
          REVIEW_REQUIRED/DELETING/DELETED/...),
        * its document is not soft-deleted.

        This is enforced here in the application layer, independent of
        Milvus compaction state.
        """
        return (
            select(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                DocumentVersion.tenant_id == self._tenant_id(),
                DocumentVersion.is_active.is_(True),
                DocumentVersion.status == IngestionState.READY,
                DocumentVersion.status.notin_(_UNSEARCHABLE_STATES),
                Document.is_deleted.is_(False),
            )
        )

    def list_searchable_versions(self) -> list[DocumentVersion]:
        return list(self.session.execute(self.searchable_versions_query()).scalars().all())

    # ------------------------------------------------------------------
    # jobs
    # ------------------------------------------------------------------
    def create_job(
        self,
        *,
        job_type: str,
        document_id: uuid.UUID | None = None,
        document_version_id: uuid.UUID | None = None,
        idempotency_key: str,
        payload: dict | None = None,
    ) -> Job:
        """Create a durable job, persisting the trusted ownership context.

        Workers revalidate the persisted context before acting (§4).
        """
        tenant_id = self._tenant_id()
        if document_id is not None:
            self.get_document(document_id)  # ownership check
        # Idempotency pre-check: a duplicate key raises ConflictError without
        # touching the session's transaction state. The UNIQUE constraint
        # remains as a backstop; in the (unlikely) race that trips it, the
        # raw IntegrityError propagates and the caller — who owns the
        # transaction — must roll back, per standard SQLAlchemy semantics.
        duplicate = (
            self.session.execute(
                select(Job.id).where(
                    Job.tenant_id == tenant_id,
                    Job.idempotency_key == idempotency_key,
                )
            ).scalar_one_or_none()
        )
        if duplicate is not None:
            raise ConflictError(
                f"job with idempotency key {idempotency_key!r} already exists "
                f"for this tenant"
            )
        job = Job(
            tenant_id=tenant_id,
            document_id=document_id,
            document_version_id=document_version_id,
            job_type=job_type,
            state=IngestionState.UPLOADED,
            idempotency_key=idempotency_key,
            trusted_user_id=self.context.user_id,
            trusted_group_id=self.context.group_id,
            trusted_roles=list(self.context.roles),
            payload=payload or {},
        )
        self.session.add(job)
        self.session.flush()
        log.info(
            "job created: job=%s type=%s document=%s version=%s group=%s",
            job.id,
            job_type,
            document_id,
            document_version_id,
            self.context.group_id,
        )
        return job

    def get_job(self, job_id: uuid.UUID) -> Job:
        job = (
            self.session.execute(
                select(Job).where(
                    Job.id == job_id, Job.tenant_id == self._tenant_id()
                )
            )
            .scalars()
            .one_or_none()
        )
        if job is None:
            raise NotFoundError(f"job {job_id} not found")
        return job

    def update_job_state(
        self, job_id: uuid.UUID, state: IngestionState, *, error: str | None = None
    ) -> Job:
        job = self.get_job(job_id)
        job.state = state
        if error is not None:
            job.error = error
        self.session.flush()
        return job

    def list_version_jobs(
        self,
        document_version_id: uuid.UUID,
        *,
        job_type: str | None = None,
        limit: int = 50,
    ) -> list[Job]:
        """List jobs for a version, newest first, tenant-scoped.

        Used by the document API's status endpoint to report the active
        version's ingestion job. Ownership is enforced via ``get_version``.
        """
        version = self.get_version(document_version_id)
        stmt = (
            select(Job)
            .where(
                Job.document_version_id == version.id,
                Job.tenant_id == self._tenant_id(),
            )
            .order_by(Job.created_at.desc())
            .limit(limit)
        )
        if job_type is not None:
            stmt = stmt.where(Job.job_type == job_type)
        return list(self.session.execute(stmt).scalars().all())

    def get_job_by_idempotency_key(self, idempotency_key: str) -> Job | None:
        """Fetch a job by its idempotency key, tenant-scoped.

        Used to report the already-queued job when a duplicate submission
        raises ``ConflictError`` (e.g. a retried DELETE).
        """
        return (
            self.session.execute(
                select(Job).where(
                    Job.tenant_id == self._tenant_id(),
                    Job.idempotency_key == idempotency_key,
                )
            )
            .scalars()
            .one_or_none()
        )

    def revalidate_job_context(self, job: Job) -> bool:
        """Re-check that the job's persisted trusted context is still valid.

        Called by workers before acting: the tenant must still exist and be
        active, and the persisted group must match the tenant the job is
        scoped to. Returns True when valid; raises otherwise (fail closed).
        """
        tenant = (
            self.session.execute(
                select(Tenant).where(
                    Tenant.id == job.tenant_id, Tenant.is_active.is_(True)
                )
            )
            .scalars()
            .one_or_none()
        )
        if tenant is None:
            raise AuthorizationError("job tenant is unknown or inactive")
        if tenant.name != job.trusted_group_id:
            raise AuthorizationError("job trusted context no longer matches tenant")
        return True

    # ------------------------------------------------------------------
    # review quarantine (§5)
    # ------------------------------------------------------------------
    def quarantine_page(
        self,
        *,
        document_id: uuid.UUID,
        document_version_id: uuid.UUID,
        job_id: uuid.UUID | None,
        page_index: int,
        page_render_ref: str,
        parse_attempts: dict,
        routing_diagnostics: dict,
    ) -> ReviewQuarantineItem:
        version = self.get_version(document_version_id)
        if version.document_id != document_id:
            raise ValueError("version does not belong to document")
        item = ReviewQuarantineItem(
            tenant_id=version.tenant_id,
            document_id=document_id,
            document_version_id=document_version_id,
            job_id=job_id,
            page_index=page_index,
            page_render_ref=page_render_ref,
            parse_attempts=parse_attempts,
            routing_diagnostics=routing_diagnostics,
        )
        self.session.add(item)
        self.session.flush()
        # Parking a page for review moves the version out of the indexable path.
        if version.status != IngestionState.REVIEW_REQUIRED:
            version.status = IngestionState.REVIEW_REQUIRED
            self.session.flush()
        log.info(
            "page quarantined: item=%s document=%s version=%s page=%d job=%s group=%s",
            item.id,
            document_id,
            document_version_id,
            page_index,
            job_id,
            self.context.group_id,
        )
        return item

    def get_quarantine_item(self, item_id: uuid.UUID) -> ReviewQuarantineItem:
        """Return the full quarantine record for ``get_review_item`` (§5)."""
        item = (
            self.session.execute(
                select(ReviewQuarantineItem).where(
                    ReviewQuarantineItem.id == item_id,
                    ReviewQuarantineItem.tenant_id == self._tenant_id(),
                )
            )
            .scalars()
            .one_or_none()
        )
        if item is None:
            raise NotFoundError(f"quarantine item {item_id} not found")
        return item

    def list_quarantine(
        self, *, status: str = "pending", limit: int = 100, offset: int = 0
    ) -> list[ReviewQuarantineItem]:
        stmt = (
            select(ReviewQuarantineItem)
            .where(
                ReviewQuarantineItem.tenant_id == self._tenant_id(),
                ReviewQuarantineItem.status == status,
            )
            .order_by(ReviewQuarantineItem.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all())

    def resolve_quarantine_item(
        self,
        item_id: uuid.UUID,
        *,
        decision: str,
        reviewed_by: str,
        detail: dict | None = None,
    ) -> ReviewQuarantineItem:
        """Record a reviewer's decision: accept | re-route | reject (§5).

        Requires the ``reviewer`` role on the trusted context.
        """
        if decision not in ("accepted", "re-routed", "rejected"):
            raise ValueError(f"invalid quarantine decision: {decision!r}")
        if not self.context.has_role("reviewer"):
            raise AuthorizationError("resolving quarantine items requires the reviewer role")
        item = self.get_quarantine_item(item_id)
        if item.status != "pending":
            raise ConflictError(f"quarantine item {item_id} is already {item.status}")
        item.status = decision
        item.reviewer_decision = detail or {}
        item.reviewed_by = reviewed_by
        item.reviewed_at = dt.datetime.now(dt.timezone.utc)
        self.session.flush()
        log.info(
            "quarantine resolved: item=%s decision=%s reviewed_by=%s group=%s",
            item_id,
            decision,
            reviewed_by,
            self.context.group_id,
        )
        return item

    def pending_quarantine_count(self, version_id: uuid.UUID) -> int:
        version = self.get_version(version_id)
        return (
            self.session.execute(
                select(func.count())
                .select_from(ReviewQuarantineItem)
                .where(
                    ReviewQuarantineItem.document_version_id == version.id,
                    ReviewQuarantineItem.status == "pending",
                )
            ).scalar()
            or 0
        )

    # ------------------------------------------------------------------
    # ACLs (narrower permissions; group-wide default when absent)
    # ------------------------------------------------------------------
    def grant(
        self,
        document_id: uuid.UUID,
        *,
        principal_type: str,
        principal_id: str,
        permission: str,
    ) -> DocumentACL:
        doc = self.get_document(document_id)
        duplicate = (
            self.session.execute(
                select(DocumentACL.id).where(
                    DocumentACL.document_id == doc.id,
                    DocumentACL.principal_type == principal_type,
                    DocumentACL.principal_id == principal_id,
                    DocumentACL.permission == permission,
                )
            ).scalar_one_or_none()
        )
        if duplicate is not None:
            raise ConflictError("ACL entry already exists")
        acl = DocumentACL(
            tenant_id=doc.tenant_id,
            document_id=doc.id,
            principal_type=principal_type,
            principal_id=principal_id,
            permission=permission,
        )
        self.session.add(acl)
        self.session.flush()
        return acl

    def check_permission(self, document_id: uuid.UUID, permission: str) -> bool:
        """Enforce document ACLs before candidates enter fusion/reranking.

        Group-wide default (§4): with no ACL rows, every member of the owning
        tenant may read. With rows present, the caller's user id or one of
        their roles must hold the permission.
        """
        doc = self.get_document(document_id)  # tenant scoping first
        rows = list(
            self.session.execute(
                select(DocumentACL).where(DocumentACL.document_id == doc.id)
            )
            .scalars()
            .all()
        )
        if not rows:
            return permission == "read"
        identities = {("user", self.context.user_id)} | {
            ("role", role) for role in self.context.roles
        }
        return any(
            (row.principal_type, row.principal_id) in identities
            and row.permission == permission
            for row in rows
        )

    # ------------------------------------------------------------------
    # admin (cross-tenant operator reads)
    # ------------------------------------------------------------------
    # SECURITY: these methods deliberately bypass the tenant predicate.
    # Every caller MUST enforce the ``admin`` role before invoking them —
    # they are only reachable via the document_api ``/v1/admin/*`` routes,
    # which gate on the JWT ``roles`` claim. Never expose them on a
    # tenant-scoped route.

    def list_all_documents(
        self,
        *,
        tenant_name: str | None = None,
        status: IngestionState | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[tuple[Document, str, IngestionState | None]]:
        """Cross-tenant document list for operators.

        Returns ``(document, tenant_name, latest_version_status)`` rows,
        newest first. ``status`` filters on the document's *latest* version
        status (a fresh upload's version is UPLOADED long before it becomes
        the active version, so latest-version status is the useful operator
        signal). Documents with no versions have ``None`` status and are
        excluded when a status filter is given.
        """
        latest_version = (
            select(
                DocumentVersion.document_id.label("document_id"),
                func.max(DocumentVersion.version_number).label("max_version_number"),
            )
            .group_by(DocumentVersion.document_id)
            .subquery("latest_version")
        )
        stmt = (
            select(Document, Tenant.name, DocumentVersion.status)
            .join(Tenant, Document.tenant_id == Tenant.id)
            .outerjoin(
                latest_version, latest_version.c.document_id == Document.id
            )
            .outerjoin(
                DocumentVersion,
                (DocumentVersion.document_id == Document.id)
                & (
                    DocumentVersion.version_number
                    == latest_version.c.max_version_number
                ),
            )
            .where(Document.is_deleted.is_(False))
            .order_by(Document.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if tenant_name is not None:
            stmt = stmt.where(Tenant.name == tenant_name)
        if status is not None:
            stmt = stmt.where(DocumentVersion.status == status)
        return list(self.session.execute(stmt).all())

    def list_all_jobs(
        self, *, tenant_name: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[tuple[Job, str]]:
        """Cross-tenant job list for operators, newest first."""
        stmt = (
            select(Job, Tenant.name)
            .join(Tenant, Job.tenant_id == Tenant.id)
            .order_by(Job.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if tenant_name is not None:
            stmt = stmt.where(Tenant.name == tenant_name)
        return list(self.session.execute(stmt).all())

    def list_tenant_names(self, *, active_only: bool = True) -> list[str]:
        """All tenant names (``group_id`` claim values), alphabetical."""
        stmt = select(Tenant.name).order_by(Tenant.name)
        if active_only:
            stmt = stmt.where(Tenant.is_active.is_(True))
        return list(self.session.execute(stmt).scalars().all())
