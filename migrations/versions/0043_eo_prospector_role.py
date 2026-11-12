"""eo_users: the Prospector role; eo_settings: the most a Prospector's Quick Start run may spend

Revision ID: 0043
Revises: 0042
Create Date: 2026-11-12 20:00:00.000000

Only objects in the `outreach` schema may be created or changed here. Never Lead Intel's tables. Existing rows are
untouched: the role list gains one value, and the new settings column has a default.
"""

import sqlalchemy as sa
from alembic import op


revision = '0043'
down_revision = '0042'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f('eo_users_role_check'), 'eo_users', schema='outreach', type_='check')
    op.create_check_constraint(op.f('eo_users_role_check'), 'eo_users', "role in ('sales', 'management', 'admin', 'prospector')", schema='outreach')
    op.add_column('eo_settings', sa.Column('prospector_run_limit_usd', sa.Numeric(10, 2), server_default='5', nullable=False), schema='outreach')
    op.create_check_constraint(op.f('eo_settings_prospector_run_limit_usd_check'), 'eo_settings', 'prospector_run_limit_usd >= 0', schema='outreach')


def downgrade() -> None:
    op.drop_constraint(op.f('eo_settings_prospector_run_limit_usd_check'), 'eo_settings', schema='outreach', type_='check')
    op.drop_column('eo_settings', 'prospector_run_limit_usd', schema='outreach')
    op.drop_constraint(op.f('eo_users_role_check'), 'eo_users', schema='outreach', type_='check')
    op.create_check_constraint(op.f('eo_users_role_check'), 'eo_users', "role in ('sales', 'management', 'admin')", schema='outreach')
