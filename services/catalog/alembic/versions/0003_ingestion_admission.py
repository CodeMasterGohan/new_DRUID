"""Transactional ingestion accounting; retained independently of document deletion."""
from alembic import op
import sqlalchemy as sa
revision = '0003_ingestion_admission'
down_revision = '0002_identity_groups'
branch_labels = depends_on = None


def upgrade():
    op.create_table('ingestion_admissions',
        sa.Column('version_id', sa.Uuid(), primary_key=True),
        sa.Column('tenant_id', sa.Uuid(), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('quota_day', sa.Date(), nullable=False),
        sa.Column('pages', sa.Integer(), nullable=False))
    op.create_index('ix_admission_tenant_day', 'ingestion_admissions', ['tenant_id', 'quota_day'])
    # Preserve today's accepted work when migrating from gateway counters.
    # Run with old API/workers drained; page_count is the server-validated count.
    op.execute("""INSERT INTO ingestion_admissions (version_id, tenant_id, quota_day, pages)
        SELECT id, tenant_id, CAST(created_at AT TIME ZONE 'UTC' AS DATE), page_count
        FROM document_versions
        WHERE page_count > 0 AND CAST(created_at AT TIME ZONE 'UTC' AS DATE)
            = CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE)""")


def downgrade():
    raise RuntimeError('Retain admission history; use a forward migration for rollback')
