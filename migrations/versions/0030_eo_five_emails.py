"""Five emails per lead: follow-ups 4 and 5

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-04 18:00:00.000000

Emails may be steps 1 to 5 (was 1 to 3), prompts may be follow-ups 4 and 5, and the settings get the generic
text of emails 4 and 5. No existing row changes: the new columns have defaults.
Only objects in the `outreach` schema may be created or changed here. Never Lead Intel's tables.
"""

import sqlalchemy as sa
from alembic import op


revision = '0030'
down_revision = '0029'
branch_labels = None
depends_on = None

OLD_KINDS = "kind in ('verify', 'hook', 'email', 'score', 'followup2', 'followup3', 'sort', 'sort_people')"
NEW_KINDS = "kind in ('verify', 'hook', 'email', 'score', 'followup2', 'followup3', 'followup4', 'followup5', 'sort', 'sort_people')"
FALLBACK_4 = "Hi {{first_name}}, one more thought on this in case the timing is better now. Happy to share how it would work for you."
FALLBACK_5 = "Hi {{first_name}}, I'll leave it here so I'm not cluttering your inbox. If it's ever useful, just reply and I'll send the details."


def upgrade() -> None:
    op.drop_constraint(op.f('eo_prompts_kind_check'), 'eo_prompts', schema='outreach', type_='check')
    op.create_check_constraint(op.f('eo_prompts_kind_check'), 'eo_prompts', NEW_KINDS, schema='outreach')
    op.drop_constraint(op.f('eo_emails_step_check'), 'eo_emails', schema='outreach', type_='check')
    op.create_check_constraint(op.f('eo_emails_step_check'), 'eo_emails', 'step between 1 and 5', schema='outreach')
    op.add_column('eo_settings', sa.Column('followup_fallback_4', sa.Text(), server_default=FALLBACK_4, nullable=False), schema='outreach')
    op.add_column('eo_settings', sa.Column('followup_fallback_5', sa.Text(), server_default=FALLBACK_5, nullable=False), schema='outreach')


def downgrade() -> None:
    op.drop_column('eo_settings', 'followup_fallback_5', schema='outreach')
    op.drop_column('eo_settings', 'followup_fallback_4', schema='outreach')
    op.drop_constraint(op.f('eo_emails_step_check'), 'eo_emails', schema='outreach', type_='check')
    op.create_check_constraint(op.f('eo_emails_step_check'), 'eo_emails', 'step between 1 and 3', schema='outreach')
    op.drop_constraint(op.f('eo_prompts_kind_check'), 'eo_prompts', schema='outreach', type_='check')
    op.create_check_constraint(op.f('eo_prompts_kind_check'), 'eo_prompts', OLD_KINDS, schema='outreach')
