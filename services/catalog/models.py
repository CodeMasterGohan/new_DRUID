"""SQLAlchemy models for the new_DRUID document catalog (spec §12).

Design notes:

* Every tenant-owned table carries ``tenant_id``. The repository layer
  (§4 tenant-scoped repository) filters on it for every query — there is no
  code path that reads another tenant's rows.
* ``IngestionState`` covers the full lifecycle from §12:
  ``UPLOADED → QUEUED → PARSING → CHUNKING → EMBEDDING → INDEXING → READY``
  plus the exceptional states ``RETRY_PENDING``, ``FAILED``,
  ``REVIEW_REQUIRED``, ``DELETING``, ``DELETED``.
* ``Document.active_version_id`` is the single pointer the retrieval path
  uses; only one version per document may have ``is_active=True`` at a time
  (enforced by :mod:`services.catalog.activation`, not just convention).
* ``ParserRevisionStats`` records the plausible chunk-count range per
  (parser, parser_revision) so version activation can assert *ranges*
  instead of exact counts (§12: counts legitimately vary between parser
  revisions).
* JSON columns use PostgreSQL JSONB in production and fall back to generic
  JSON on SQLite (used by the unit tests).
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# JSONB on PostgreSQL, plain JSON elsewhere (SQLite in tests).
FlexibleJSON = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class IngestionState(str, enum.Enum):
    """Document-version / job lifecycle states (§12)."""

    UPLOADED = "UPLOADED"
    QUEUED = "QUEUED"
    PARSING = "PARSING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"
    READY = "READY"
    RETRY_PENDING = "RETRY_PENDING"
    FAILED = "FAILED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    DELETING = "DELETING"
    DELETED = "DELETED"


class Tenant(Base):
    """A tenant (authorization boundary). Maps to ``group_id`` in §4."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Document(Base):
    """A logical document owned by a tenant.

    ``id`` is the opaque ``document_id`` handed to clients. Original
    filenames are display metadata only and never storage paths (§5).
    """

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    # Pointer to the currently searchable version. The FK is added after
    # document_versions exists (use_alter) because the two tables reference
    # each other.
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", use_alter=True, ondelete="SET NULL"),
        nullable=True,
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class DocumentVersion(Base):
    """One immutable parse/index attempt of a document.

    Only versions with ``is_active=True`` AND ``status=READY`` are searchable;
    see :func:`services.catalog.repository.CatalogRepository.searchable_versions_query`
    for the hard filter (§10 step 3, §12).
    """

    __tablename__ = "document_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalized so every version query can be tenant-scoped without a join.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[IngestionState] = mapped_column(
        SAEnum(IngestionState, name="ingestion_state"),
        nullable=False,
        default=IngestionState.UPLOADED,
    )
    parser: Mapped[str] = mapped_column(String(32), nullable=False)  # native | olmocr
    parser_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count_actual: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    activated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_version_doc_number"),
        Index("ix_versions_tenant_status", "tenant_id", "status"),
        Index("ix_versions_tenant_active", "tenant_id", "is_active"),
    )


class Job(Base):
    """Durable background work (ingestion or deletion).

    The trusted ownership context is persisted on the job so workers can
    revalidate it before acting (§4: "Background jobs — persist and revalidate
    trusted ownership context"). ``idempotency_key`` is unique per tenant so
    retries never create duplicates (§12).
    """

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="SET NULL"), nullable=True
    )
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)  # ingestion | deletion
    state: Mapped[IngestionState] = mapped_column(
        SAEnum(IngestionState, name="ingestion_state"),
        nullable=False,
        default=IngestionState.UPLOADED,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # Persisted trusted context (never taken from the job payload at runtime).
    trusted_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    trusted_group_id: Mapped[str] = mapped_column(String(255), nullable=False)
    trusted_roles: Mapped[list] = mapped_column(FlexibleJSON, nullable=False, default=list)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    payload: Mapped[dict] = mapped_column(FlexibleJSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_jobs_tenant_idempotency"
        ),
        Index("ix_jobs_tenant_state", "tenant_id", "state"),
    )


class ReviewQuarantineItem(Base):
    """One page parked in ``REVIEW_REQUIRED`` (§5 review queue).

    Holds everything a reviewer needs: the page render reference, both parse
    attempts (native + visual), and the routing diagnostics (per-signal
    values). Unreviewed items are never searchable.
    """

    __tablename__ = "review_quarantine"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_render_ref: Mapped[str] = mapped_column(
        String(1024), nullable=False
    )  # MinIO object-storage key for the page render
    parse_attempts: Mapped[dict] = mapped_column(
        FlexibleJSON, nullable=False, default=dict
    )  # {"native": {...}, "visual": {...}}
    routing_diagnostics: Mapped[dict] = mapped_column(
        FlexibleJSON, nullable=False, default=dict
    )  # per-signal values from the page router
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )  # pending | accepted | re-routed | rejected
    reviewer_decision: Mapped[dict | None] = mapped_column(FlexibleJSON, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_quarantine_tenant_status", "tenant_id", "status"),
        Index("ix_quarantine_version", "document_version_id"),
    )


class DocumentACL(Base):
    """Optional document-level access control entries.

    The initial permissions model is group-wide document access (§4): when no
    ACL rows exist for a document, every member of the owning tenant may read
    it. When rows exist, they are enforced before candidates enter fusion and
    reranking.
    """

    __tablename__ = "document_acls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    principal_type: Mapped[str] = mapped_column(String(32), nullable=False)  # group | user | role
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    permission: Mapped[str] = mapped_column(String(32), nullable=False)  # read | write | admin
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "principal_type",
            "principal_id",
            "permission",
            name="uq_acl_entry",
        ),
    )


class ParserRevisionStats(Base):
    """Plausible chunk-count range per (parser, parser_revision).

    Recorded from the M1 eval corpus / production observations. Version
    activation asserts the actual chunk count falls inside
    ``[min_chunks_per_page, max_chunks_per_page] * page_count`` — ranges, not
    exact counts, because counts legitimately vary between revisions (§12).
    """

    __tablename__ = "parser_revision_stats"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    parser: Mapped[str] = mapped_column(String(32), nullable=False)
    parser_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    min_chunks_per_page: Mapped[float] = mapped_column(Float, nullable=False)
    max_chunks_per_page: Mapped[float] = mapped_column(Float, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("parser", "parser_revision", name="uq_parser_revision"),
    )
