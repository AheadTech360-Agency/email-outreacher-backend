"""Prompt kinds for smart list sorting: sort (companies, with ICP fit) and sort_people

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-02 21:00:00.000000

Only the allowed values of eo_prompts.kind change (approved 2026-11-02); no rows are touched.
Only objects in the `outreach` schema may be created or changed here. Never Lead Intel's tables.
"""

from alembic import op


revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None

OLD = "kind in ('verify', 'hook', 'email', 'score', 'followup2', 'followup3')"
NEW = "kind in ('verify', 'hook', 'email', 'score', 'followup2', 'followup3', 'sort', 'sort_people')"


def upgrade() -> None:
    op.drop_constraint(op.f('eo_prompts_kind_check'), 'eo_prompts', schema='outreach', type_='check')
    op.create_check_constraint(op.f('eo_prompts_kind_check'), 'eo_prompts', NEW, schema='outreach')


def downgrade() -> None:
    op.drop_constraint(op.f('eo_prompts_kind_check'), 'eo_prompts', schema='outreach', type_='check')
    op.create_check_constraint(op.f('eo_prompts_kind_check'), 'eo_prompts', OLD, schema='outreach')
