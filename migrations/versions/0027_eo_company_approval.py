"""Company approval: verified companies under the workspace's threshold wait for a person to approve or reject them

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-02 23:30:00.000000

Only objects in the `outreach` schema may be created or changed here. Never Lead Intel's tables.
- eo_companies.status may be 'review' (waiting for approval).
- eo_quickstart_runs.status may be 'waiting' (paused until the run's companies are approved).
- eo_settings gets company_threshold and company_auto_approve, like the email score threshold and auto-send.
"""

from alembic import op
import sqlalchemy as sa


revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f('eo_companies_status_check'), 'eo_companies', schema='outreach', type_='check')
    op.create_check_constraint(op.f('eo_companies_status_check'), 'eo_companies',
                               "status in ('found', 'review', 'qualified', 'not_fit', 'error')", schema='outreach')
    op.drop_constraint(op.f('eo_quickstart_runs_status_check'), 'eo_quickstart_runs', schema='outreach', type_='check')
    op.create_check_constraint(op.f('eo_quickstart_runs_status_check'), 'eo_quickstart_runs',
                               "status in ('queued', 'running', 'waiting', 'done', 'stopped', 'failed', 'cancelled')", schema='outreach')
    op.add_column('eo_settings', sa.Column('company_threshold', sa.SmallInteger(), server_default='60', nullable=False), schema='outreach')
    op.add_column('eo_settings', sa.Column('company_auto_approve', sa.Boolean(), server_default='true', nullable=False), schema='outreach')


def downgrade() -> None:
    op.drop_column('eo_settings', 'company_auto_approve', schema='outreach')
    op.drop_column('eo_settings', 'company_threshold', schema='outreach')
    op.drop_constraint(op.f('eo_quickstart_runs_status_check'), 'eo_quickstart_runs', schema='outreach', type_='check')
    op.create_check_constraint(op.f('eo_quickstart_runs_status_check'), 'eo_quickstart_runs',
                               "status in ('queued', 'running', 'done', 'stopped', 'failed', 'cancelled')", schema='outreach')
    op.drop_constraint(op.f('eo_companies_status_check'), 'eo_companies', schema='outreach', type_='check')
    op.create_check_constraint(op.f('eo_companies_status_check'), 'eo_companies',
                               "status in ('found', 'qualified', 'not_fit', 'error')", schema='outreach')
