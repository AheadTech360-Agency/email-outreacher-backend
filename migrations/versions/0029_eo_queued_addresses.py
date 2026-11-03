"""Every email address ever queued for sending, so nobody is queued twice

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-03 15:00:00.000000

Only objects in the `outreach` schema may be created or changed here. Never Lead Intel's tables.
One row per address (lower case), for good: it outlives the lead and the email it was queued with. The
primary key is what makes it hold, even for two queue runs at once. Filled with every address already queued.
"""

from alembic import op
import sqlalchemy as sa


revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'eo_queued_addresses',
        sa.Column('email', sa.Text(), nullable=False),
        sa.Column('workspace_id', sa.Uuid(), nullable=True),
        sa.Column('email_id', sa.Uuid(), nullable=True),
        sa.Column('campaign_id', sa.Text(), nullable=True),
        sa.Column('queued_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id'], ['outreach.eo_workspaces.id'], name=op.f('eo_queued_addresses_workspace_id_fkey'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('email', name=op.f('eo_queued_addresses_pkey')),
        schema='outreach',
    )
    op.execute("""
        insert into outreach.eo_queued_addresses (email, workspace_id, email_id, campaign_id, queued_at)
        select distinct on (lower(l.email)) lower(l.email), e.workspace_id, e.id, e.sequencer_campaign_id, coalesce(e.queued_at, now())
        from outreach.eo_emails e join outreach.eo_leads l on l.id = e.lead_id
        where e.step = 1 and e.sequencer_campaign_id is not null and l.email is not null and l.email <> ''
        order by lower(l.email), e.queued_at
        on conflict do nothing
    """)


def downgrade() -> None:
    op.drop_table('eo_queued_addresses', schema='outreach')
