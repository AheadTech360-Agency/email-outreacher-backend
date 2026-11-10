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
    op.create_index("eo_warmup_messages_from_idx", "eo_warmup_messages", ["from_account_id", "sent_at"], schema=S)
    op.create_index("eo_warmup_messages_to_idx", "eo_warmup_messages", ["to_account_id", "sent_at"], schema=S)


def downgrade():
    op.drop_table("eo_warmup_messages", schema=S)
    for name in ("eo_sender_accounts_warmup_reply_percent_check", "eo_sender_accounts_warmup_daily_target_check", "eo_sender_accounts_pool_check"):
        op.drop_constraint(name, "eo_sender_accounts", schema=S, type_="check")
    for column in ("warmup_started_at", "warmup_reply_percent", "warmup_daily_target", "pool"):
        op.drop_column("eo_sender_accounts", column, schema=S)
