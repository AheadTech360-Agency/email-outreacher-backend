"""eo_companies: companies found by a LinkedIn company search (Quick Start's third option)

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-12 14:00:00.000000

Only objects in the `outreach` schema may be created or changed here. Never Lead Intel's tables. The only change is
which sources a company may have: existing rows are untouched.
"""

from alembic import op


revision = '0042'
down_revision = '0041'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f('eo_companies_source_check'), 'eo_companies', schema='outreach', type_='check')
    op.create_check_constraint(
        op.f('eo_companies_source_check'), 'eo_companies', "source in ('lead_intel', 'apollo', 'google_maps', 'linkedin')", schema='outreach'
    )


def downgrade() -> None:
    op.drop_constraint(op.f('eo_companies_source_check'), 'eo_companies', schema='outreach', type_='check')
    op.create_check_constraint(
        op.f('eo_companies_source_check'), 'eo_companies', "source in ('lead_intel', 'apollo', 'google_maps')", schema='outreach'
    )
