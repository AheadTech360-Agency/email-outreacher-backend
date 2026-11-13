"""eo_users: what a Prospector may do (searches, posts read, what the analysis reads and writes)

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-13 15:00:00.000000

Only our eo_ tables. A new column with a default: existing rows are untouched, and `{}` means what Prospectors could do
before (schemas.ProspectorPermissions' defaults). The schema is the one this database uses (models.TABLE_SCHEMA:
`outreach` in the shared database, the default schema in Coldbrew's own).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from models import TABLE_SCHEMA

revision = '0045'
down_revision = '0044'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('eo_users', sa.Column('permissions', JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False), schema=TABLE_SCHEMA)


def downgrade() -> None:
    op.drop_column('eo_users', 'permissions', schema=TABLE_SCHEMA)
