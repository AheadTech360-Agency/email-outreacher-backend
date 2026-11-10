"""LinkedIn and Instagram accounts connected through Unipile, and which mailbox each belongs to.

Only adds one table. Unipile holds the login itself; this keeps its account id, what to show, its status, and the
mailbox (eo_sender_accounts) it goes with.
"""

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

S = "outreach"


def upgrade():
    op.create_table(
        "eo_social_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("unipile_account_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, server_default=""),
        sa.Column("username", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="ok"),
        sa.Column("sender_account_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_sender_accounts.id", ondelete="SET NULL")),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("provider in ('linkedin', 'instagram')", name="eo_social_accounts_provider_check"),
        sa.CheckConstraint("status in ('ok', 'connecting', 'credentials', 'permissions', 'error', 'stopped')", name="eo_social_accounts_status_check"),
        sa.UniqueConstraint("unipile_account_id", name="eo_social_accounts_unipile_account_id_key"),
        schema=S,
    )  # fmt: skip
    op.create_index("eo_social_accounts_workspace_id_idx", "eo_social_accounts", ["workspace_id"], schema=S)
    op.create_index("eo_social_accounts_sender_account_id_idx", "eo_social_accounts", ["sender_account_id"], schema=S)
    # A sign-in someone started from a workspace: Sync only adopts a new account for the workspace that asked for it.
    op.create_table(
        "eo_social_connects",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("provider in ('linkedin', 'instagram')", name="eo_social_connects_provider_check"),
        schema=S,
    )  # fmt: skip
    op.create_index("eo_social_connects_provider_idx", "eo_social_connects", ["provider", "started_at"], schema=S)


def downgrade():
    op.drop_table("eo_social_connects", schema=S)
    op.drop_table("eo_social_accounts", schema=S)
