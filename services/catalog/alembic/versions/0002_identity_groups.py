"""OIDC users, internal group memberships and document creator.

Revision ID: 0002_identity_groups
Revises: 0001_initial_schema
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0002_identity_groups"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "druid_users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("subject", sa.String(255), nullable=False, unique=True),
        sa.Column("username", sa.String(255), nullable=False, server_default=""),
        sa.Column("email", sa.String(320)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_system_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "group_memberships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("druid_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "tenant_id", name="uq_user_tenant_membership"),
    )
    op.create_index("ix_memberships_user", "group_memberships", ["user_id"])
    op.create_index("ix_memberships_tenant", "group_memberships", ["tenant_id"])
    op.add_column("documents", sa.Column("created_by", sa.String(255), nullable=True))
    op.create_index("ix_documents_created_by", "documents", ["created_by"])


def downgrade() -> None:
    op.drop_index("ix_documents_created_by", table_name="documents")
    op.drop_column("documents", "created_by")
    op.drop_index("ix_memberships_tenant", table_name="group_memberships")
    op.drop_index("ix_memberships_user", table_name="group_memberships")
    op.drop_table("group_memberships")
    op.drop_table("druid_users")
