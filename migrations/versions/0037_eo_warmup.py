"""The email warmer: each mailbox is in the sending pool (campaigns send from it) or the warming pool (it trades
warm-up emails with the other warming mailboxes, which open them, rescue them from spam, star some and reply to
some). eo_warmup_messages records every warm-up email, once, by its Message-ID.

Only adds: columns to eo_sender_accounts (every existing mailbox stays in the sending pool) and one new table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

S = "outreach"


def upgrade():
    op.add_column("eo_sender_accounts", sa.Column("pool", sa.Text(), nullable=False, server_default="sending"), schema=S)
    op.add_column("eo_sender_accounts", sa.Column("warmup_daily_target", sa.SmallInteger(), nullable=False, server_default="10"), schema=S)
    op.add_column("eo_sender_accounts", sa.Column("warmup_reply_percent", sa.SmallInteger(), nullable=False, server_default="30"), schema=S)
    op.add_column("eo_sender_accounts", sa.Column("warmup_started_at", sa.DateTime(timezone=True)), schema=S)
    op.create_check_constraint("eo_sender_accounts_pool_check", "eo_sender_accounts", "pool in ('sending', 'warming')", schema=S)
    op.create_check_constraint("eo_sender_accounts_warmup_daily_target_check", "eo_sender_accounts", "warmup_daily_target between 1 and 100", schema=S)
    op.create_check_constraint("eo_sender_accounts_warmup_reply_percent_check", "eo_sender_accounts", "warmup_reply_percent between 0 and 100", schema=S)

    op.create_table(
        "eo_warmup_messages",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_account_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_sender_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("to_account_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_sender_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        # A reply's parent warm-up email, so a thread stays a thread.
        sa.Column("reply_to_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_warmup_messages.id", ondelete="SET NULL")),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="sent"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # What the receiving mailbox did with it: where it landed, and when it was opened, starred and replied to.
        sa.Column("landed_in", sa.Text()),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("starred", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("rescued_from_spam", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("replied_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint("status in ('sent', 'dry_run', 'failed')", name="eo_warmup_messages_status_check"),
        sa.CheckConstraint("landed_in is null or landed_in in ('inbox', 'spam')", name="eo_warmup_messages_landed_in_check"),
        sa.UniqueConstraint("message_id", name="eo_warmup_messages_message_id_key"),
        schema=S,
    )  # fmt: skip
    op.create_index("eo_warmup_messages_workspace_id_idx", "eo_warmup_messages", ["workspace_id"], schema=S)

    # Each workspace's sending limits and warm-up settings (the dashboard's Settings page). A workspace without a row
    # uses the defaults. Live sending stays a server switch (OWN_SENDING_LIVE, OWN_WARMUP_LIVE): these can only hold back.
    op.create_table(
        "eo_own_sending_settings",
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_workspaces.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("sending_paused", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("default_daily_limit", sa.SmallInteger(), nullable=False, server_default="30"),
        sa.Column("workspace_daily_cap", sa.Integer()),
        sa.Column("min_gap_minutes", sa.SmallInteger(), nullable=False, server_default="5"),
        sa.Column("max_gap_minutes", sa.SmallInteger(), nullable=False, server_default="15"),
        sa.Column("warmup_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("warmup_timezone", sa.Text(), nullable=False, server_default="America/New_York"),
        sa.Column("warmup_start", sa.Text(), nullable=False, server_default="09:00"),
        sa.Column("warmup_end", sa.Text(), nullable=False, server_default="19:00"),
        sa.Column("warmup_days", sa.ARRAY(sa.SmallInteger()), nullable=False, server_default="{1,2,3,4,5,6,7}"),
        sa.Column("warmup_start_per_day", sa.SmallInteger(), nullable=False, server_default="3"),
        sa.Column("warmup_ramp_per_day", sa.SmallInteger(), nullable=False, server_default="2"),
        sa.Column("warmup_default_target", sa.SmallInteger(), nullable=False, server_default="20"),
        sa.Column("warmup_default_reply_percent", sa.SmallInteger(), nullable=False, server_default="30"),
        sa.Column("warmup_star_percent", sa.SmallInteger(), nullable=False, server_default="25"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("default_daily_limit between 1 and 500", name="eo_own_sending_settings_default_daily_limit_check"),
        sa.CheckConstraint("workspace_daily_cap is null or workspace_daily_cap between 1 and 100000", name="eo_own_sending_settings_workspace_daily_cap_check"),
        sa.CheckConstraint("min_gap_minutes between 1 and 240 and max_gap_minutes between min_gap_minutes and 240", name="eo_own_sending_settings_gap_check"),
        sa.CheckConstraint("warmup_start_per_day between 1 and 50 and warmup_ramp_per_day between 0 and 20", name="eo_own_sending_settings_ramp_check"),
        sa.CheckConstraint("warmup_default_target between 1 and 100", name="eo_own_sending_settings_warmup_default_target_check"),
        sa.CheckConstraint("warmup_default_reply_percent between 0 and 100 and warmup_star_percent between 0 and 100", name="eo_own_sending_settings_percent_check"),
        schema=S,
    )  # fmt: skip
    op.create_index("eo_warmup_messages_from_idx", "eo_warmup_messages", ["from_account_id", "sent_at"], schema=S)
    op.create_index("eo_warmup_messages_to_idx", "eo_warmup_messages", ["to_account_id", "sent_at"], schema=S)


def downgrade():
    op.drop_table("eo_own_sending_settings", schema=S)
    op.drop_table("eo_warmup_messages", schema=S)
    for name in ("eo_sender_accounts_warmup_reply_percent_check", "eo_sender_accounts_warmup_daily_target_check", "eo_sender_accounts_pool_check"):
        op.drop_constraint(name, "eo_sender_accounts", schema=S, type_="check")
    for column in ("warmup_started_at", "warmup_reply_percent", "warmup_daily_target", "pool"):
        op.drop_column("eo_sender_accounts", column, schema=S)
