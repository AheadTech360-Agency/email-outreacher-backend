"""Daily limits for LinkedIn and Instagram: a default per network in the workspace's settings, and an optional limit
on each account that replaces it.

Only adds columns to two Coldbrew tables (eo_own_sending_settings, eo_social_accounts); nothing existing changes.
"""

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

S = "outreach"
SETTINGS = "eo_own_sending_settings"
ACCOUNTS = "eo_social_accounts"


def upgrade():
    for name, default in (("linkedin_daily_invites", "20"), ("linkedin_weekly_invites", "100"), ("linkedin_daily_messages", "50"),
                          ("instagram_daily_messages", "30"), ("social_min_gap_minutes", "3"), ("social_max_gap_minutes", "10")):  # fmt: skip
        op.add_column(SETTINGS, sa.Column(name, sa.SmallInteger(), nullable=False, server_default=default), schema=S)
    op.create_check_constraint("eo_own_sending_settings_social_limits", SETTINGS,
                               "linkedin_daily_invites between 1 and 100 and linkedin_weekly_invites between linkedin_daily_invites and 500 "
                               "and linkedin_daily_messages between 1 and 250 and instagram_daily_messages between 1 and 200", schema=S)  # fmt: skip
    op.create_check_constraint("eo_own_sending_settings_social_gap", SETTINGS,
                               "social_min_gap_minutes between 1 and 240 and social_max_gap_minutes between social_min_gap_minutes and 240", schema=S)  # fmt: skip
    op.add_column(ACCOUNTS, sa.Column("daily_invite_limit", sa.SmallInteger()), schema=S)
    op.add_column(ACCOUNTS, sa.Column("daily_message_limit", sa.SmallInteger()), schema=S)
    op.create_check_constraint("eo_social_accounts_limits", ACCOUNTS,
                               "(daily_invite_limit is null or daily_invite_limit between 1 and 100) "
                               "and (daily_message_limit is null or daily_message_limit between 1 and 250)", schema=S)  # fmt: skip


def downgrade():
    op.drop_constraint("eo_social_accounts_limits", ACCOUNTS, schema=S)
    op.drop_column(ACCOUNTS, "daily_message_limit", schema=S)
    op.drop_column(ACCOUNTS, "daily_invite_limit", schema=S)
    op.drop_constraint("eo_own_sending_settings_social_gap", SETTINGS, schema=S)
    op.drop_constraint("eo_own_sending_settings_social_limits", SETTINGS, schema=S)
    for name in ("linkedin_daily_invites", "linkedin_weekly_invites", "linkedin_daily_messages", "instagram_daily_messages",
                 "social_min_gap_minutes", "social_max_gap_minutes"):  # fmt: skip
        op.drop_column(SETTINGS, name, schema=S)
