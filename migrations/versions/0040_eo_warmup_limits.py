"""Warm-up limits of their own, apart from sending's: a daily cap for all of a workspace's warming mailboxes together,
and the shortest gap between one mailbox's warm-up emails.

Only adds two columns to a Coldbrew table (eo_own_sending_settings); nothing existing changes.
"""

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

S = "outreach"
T = "eo_own_sending_settings"


def upgrade():
    op.add_column(T, sa.Column("warmup_workspace_daily_cap", sa.Integer()), schema=S)
    op.add_column(T, sa.Column("warmup_min_gap_minutes", sa.SmallInteger(), nullable=False, server_default="15"), schema=S)
    op.create_check_constraint("eo_own_sending_settings_warmup_limits", T,
                               "(warmup_workspace_daily_cap is null or warmup_workspace_daily_cap between 1 and 100000) "
                               "and warmup_min_gap_minutes between 1 and 240", schema=S)  # fmt: skip


def downgrade():
    op.drop_constraint("eo_own_sending_settings_warmup_limits", T, schema=S)
    op.drop_column(T, "warmup_min_gap_minutes", schema=S)
    op.drop_column(T, "warmup_workspace_daily_cap", schema=S)
