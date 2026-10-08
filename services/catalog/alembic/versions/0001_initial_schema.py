"""Initial catalog schema (spec §12).

Revision ID: 0001_initial_schema
Revises: None
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

ingestion_state = sa.Enum(
    "UPLOADED",
    "QUEUED",
    "PARSING",
    "CHUNKING",
    "EMBEDDING",
    "INDEXING",
    "READY",
    "RETRY_PENDING",
    "FAILED",
    "REVIEW_REQUIRED",
    "DELETING",
    "DELETED",
    name="ingestion_state",
)


def upgrade() -> None:
    ingestion_state.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("name", sa.String(255), unique=True, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # documents is created without active_version_id's FK first; the FK is
    # added after document_versions exists (the two tables reference each other).
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("display_name", sa.String(1024), nullable=False),
        sa.Column("content_checksum", sa.String(128), nullable=False),
        sa.Column("active_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "document_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("document_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", ingestion_state, nullable=False, server_default="UPLOADED"),
        sa.Column("parser", sa.String(32), nullable=False),
        sa.Column("parser_revision", sa.String(64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count_actual", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("document_id", "version_number", name="uq_version_doc_number"),
    )
    op.create_index("ix_versions_tenant_status", "document_versions", ["tenant_id", "status"])
    op.create_index("ix_versions_tenant_active", "document_versions", ["tenant_id", "is_active"])

    op.create_foreign_key(
        "fk_documents_active_version",
        "documents",
        "document_versions",
        ["active_version_id"],
        ["id"],
        use_alter=True,
        ondelete="SET NULL",
    )

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("documents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("document_versions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("state", ingestion_state, nullable=False, server_default="UPLOADED"),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("trusted_user_id", sa.String(255), nullable=False),
        sa.Column("trusted_group_id", sa.String(255), nullable=False),
        sa.Column("trusted_roles", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_jobs_tenant_idempotency"),
    )
    op.create_index("ix_jobs_tenant_state", "jobs", ["tenant_id", "state"])

    op.create_table(
        "review_quarantine",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("page_index", sa.Integer(), nullable=False),
        sa.Column("page_render_ref", sa.String(1024), nullable=False),
        sa.Column("parse_attempts", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("routing_diagnostics", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("reviewer_decision", postgresql.JSONB(), nullable=True),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_quarantine_tenant_status", "review_quarantine", ["tenant_id", "status"])
    op.create_index("ix_quarantine_version", "review_quarantine", ["document_version_id"])

    op.create_table(
        "document_acls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("principal_type", sa.String(32), nullable=False),
        sa.Column("principal_id", sa.String(255), nullable=False),
        sa.Column("permission", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "document_id", "principal_type", "principal_id", "permission",
            name="uq_acl_entry",
        ),
    )

    op.create_table(
        "parser_revision_stats",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("parser", sa.String(32), nullable=False),
        sa.Column("parser_revision", sa.String(64), nullable=False),
        sa.Column("min_chunks_per_page", sa.Float(), nullable=False),
        sa.Column("max_chunks_per_page", sa.Float(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("parser", "parser_revision", name="uq_parser_revision"),
    )


def downgrade() -> None:
    op.drop_table("parser_revision_stats")
    op.drop_table("document_acls")
    op.drop_table("review_quarantine")
    op.drop_table("jobs")
    op.drop_constraint("fk_documents_active_version", "documents", type_="foreignkey")
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_table("tenants")
    ingestion_state.drop(op.get_bind(), checkfirst=True)
