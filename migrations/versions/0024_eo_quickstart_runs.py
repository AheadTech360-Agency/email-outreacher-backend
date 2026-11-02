"""Quick Start runs: an ICP taken from a Google Maps search to drafts waiting for review

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-02 12:00:00.000000

Only objects in the `outreach` schema may be created or changed here. Never Lead Intel's tables.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'eo_quickstart_runs',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('workspace_id', sa.Uuid(), nullable=False),
        sa.Column('icp_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.Text(), server_default='queued', nullable=False),
        sa.Column('step', sa.Text(), server_default='find', nullable=False),
        sa.Column('settings', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('progress', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('company_ids', postgresql.ARRAY(sa.Uuid()), server_default='{}', nullable=False),
        sa.Column('lead_ids', postgresql.ARRAY(sa.Uuid()), server_default='{}', nullable=False),
        sa.Column('job_ids', postgresql.ARRAY(sa.Uuid()), server_default='{}', nullable=False),
        sa.Column('spend_limit_usd', sa.Numeric(12, 2), nullable=False),
        sa.Column('spent_usd', sa.Numeric(12, 4), server_default='0', nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('busy_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status in ('queued', 'running', 'done', 'stopped', 'failed', 'cancelled')", name=op.f('eo_quickstart_runs_status_check')),
        sa.ForeignKeyConstraint(['workspace_id'], ['outreach.eo_workspaces.id'], name=op.f('eo_quickstart_runs_workspace_id_fkey'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['outreach.eo_users.id'], name=op.f('eo_quickstart_runs_created_by_fkey'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('eo_quickstart_runs_pkey')),
        schema='outreach',
    )
    op.create_index('eo_quickstart_runs_workspace_idx', 'eo_quickstart_runs', ['workspace_id', 'created_at'], schema='outreach')
    op.create_index(op.f('eo_quickstart_runs_workspace_id_idx'), 'eo_quickstart_runs', ['workspace_id'], schema='outreach')


def downgrade() -> None:
    op.drop_index(op.f('eo_quickstart_runs_workspace_id_idx'), table_name='eo_quickstart_runs', schema='outreach')
    op.drop_index('eo_quickstart_runs_workspace_idx', table_name='eo_quickstart_runs', schema='outreach')
    op.drop_table('eo_quickstart_runs', schema='outreach')
