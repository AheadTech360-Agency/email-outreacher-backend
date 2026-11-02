"""Smart lists: companies and people sorted into high-level groups, with a draft base ICP per company list

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-02 18:00:00.000000

Only objects in the `outreach` schema may be created or changed here. Never Lead Intel's tables.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'eo_smart_lists',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('workspace_id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('name_key', sa.Text(), nullable=False),
        sa.Column('icp_draft', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('lead_intel_icp_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("kind in ('company', 'person')", name=op.f('eo_smart_lists_kind_check')),
        sa.ForeignKeyConstraint(['workspace_id'], ['outreach.eo_workspaces.id'], name=op.f('eo_smart_lists_workspace_id_fkey'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('eo_smart_lists_pkey')),
        sa.UniqueConstraint('workspace_id', 'kind', 'name_key', name=op.f('eo_smart_lists_workspace_id_kind_name_key_key')),
        schema='outreach',
    )
    op.create_index(op.f('eo_smart_lists_workspace_id_idx'), 'eo_smart_lists', ['workspace_id'], schema='outreach')
    op.create_table(
        'eo_smart_list_members',
        sa.Column('workspace_id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('member_id', sa.Uuid(), nullable=False),
        sa.Column('list_id', sa.Uuid(), nullable=False),
        sa.Column('dedupe_key', sa.Text(), nullable=False),
        sa.Column('reason', sa.Text(), server_default='', nullable=False),
        sa.Column('sorted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("kind in ('company', 'person')", name=op.f('eo_smart_list_members_kind_check')),
        sa.ForeignKeyConstraint(['workspace_id'], ['outreach.eo_workspaces.id'], name=op.f('eo_smart_list_members_workspace_id_fkey'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['list_id'], ['outreach.eo_smart_lists.id'], name=op.f('eo_smart_list_members_list_id_fkey'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('workspace_id', 'kind', 'member_id', name=op.f('eo_smart_list_members_pkey')),
        schema='outreach',
    )
    op.create_index(op.f('eo_smart_list_members_list_id_idx'), 'eo_smart_list_members', ['list_id'], schema='outreach')
    op.create_index('eo_smart_list_members_key_idx', 'eo_smart_list_members', ['workspace_id', 'kind', 'dedupe_key'], schema='outreach')


def downgrade() -> None:
    op.drop_index('eo_smart_list_members_key_idx', table_name='eo_smart_list_members', schema='outreach')
    op.drop_index(op.f('eo_smart_list_members_list_id_idx'), table_name='eo_smart_list_members', schema='outreach')
    op.drop_table('eo_smart_list_members', schema='outreach')
    op.drop_index(op.f('eo_smart_lists_workspace_id_idx'), table_name='eo_smart_lists', schema='outreach')
    op.drop_table('eo_smart_lists', schema='outreach')
