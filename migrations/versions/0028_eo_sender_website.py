"""The sender's website, shown under the sender's name in place of a postal address

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-03 12:00:00.000000

Only objects in the `outreach` schema may be created or changed here. Never Lead Intel's tables.
sender_address stays (no longer used), so nothing saved is lost.
"""

from alembic import op
import sqlalchemy as sa


revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('eo_settings', sa.Column('sender_website', sa.Text(), server_default='', nullable=False), schema='outreach')


def downgrade() -> None:
    op.drop_column('eo_settings', 'sender_website', schema='outreach')
