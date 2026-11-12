"""eo_access: which ICPs, products and smart lists a Prospector may see, and edit

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-12 22:00:00.000000

Only objects in the `outreach` schema may be created or changed here. Never Lead Intel's tables. A new table only:
existing rows are untouched.
"""

import sqlalchemy as sa
from alembic import op


revision = '0044'
down_revision = '0043'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'eo_access',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('workspace_id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('resource_id', sa.Uuid(), nullable=False),
        sa.Column('can_edit', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('is_owner', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('granted_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("kind in ('icp', 'product', 'smart_list')", name=op.f('eo_access_kind_check')),
        sa.ForeignKeyConstraint(['user_id'], ['outreach.eo_users.id'], name=op.f('eo_access_user_id_fkey'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['outreach.eo_workspaces.id'], name=op.f('eo_access_workspace_id_fkey'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['granted_by'], ['outreach.eo_users.id'], name=op.f('eo_access_granted_by_fkey'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('eo_access_pkey')),
        sa.UniqueConstraint('user_id', 'kind', 'resource_id', name=op.f('eo_access_user_id_kind_resource_id_key')),
        schema='outreach',
    )
    op.create_index('eo_access_resource_idx', 'eo_access', ['kind', 'resource_id'], schema='outreach')
    op.create_index(op.f('eo_access_workspace_id_idx'), 'eo_access', ['workspace_id'], schema='outreach')


def downgrade() -> None:
    op.drop_index(op.f('eo_access_workspace_id_idx'), table_name='eo_access', schema='outreach')
    op.drop_index('eo_access_resource_idx', table_name='eo_access', schema='outreach')
    op.drop_table('eo_access', schema='outreach')
