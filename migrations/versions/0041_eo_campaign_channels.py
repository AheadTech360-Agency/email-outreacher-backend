"""Campaigns on three channels: email, LinkedIn and Instagram, and a prompt for each social message.

Changes only Coldbrew tables, and only adds: a channel on each campaign (existing ones are email), an action on each
step (existing ones are email), the LinkedIn or Instagram account an enrollment sends from and the chat it's in, which
social account a step run used, and three prompt kinds. Nothing existing changes meaning.
"""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

S = "outreach"
OLD_KINDS = "kind in ('verify', 'hook', 'email', 'score', 'followup2', 'followup3', 'followup4', 'followup5', 'sort', 'sort_people', 'product', 'analysis')"
# Named twice over by the models' naming convention when 0036 made it.
OLD_RUN_CHECK = "eo_step_runs_eo_step_runs_status_check_check"
NEW_KINDS = OLD_KINDS[:-1] + ", 'linkedin_invite', 'linkedin_message', 'instagram_message')"


def upgrade():
    op.add_column("eo_sequences", sa.Column("channel", sa.Text(), nullable=False, server_default="email"), schema=S)
    op.create_check_constraint(op.f("eo_sequences_channel_check"), "eo_sequences", "channel in ('email', 'linkedin', 'instagram')", schema=S)
    op.add_column("eo_sequence_steps", sa.Column("action", sa.Text(), nullable=False, server_default="email"), schema=S)
    op.create_check_constraint(op.f("eo_sequence_steps_action_check"), "eo_sequence_steps", "action in ('email', 'invite', 'message')", schema=S)
    op.add_column("eo_enrollments", sa.Column("social_account_id", sa.Uuid(),
                                              sa.ForeignKey(f"{S}.eo_social_accounts.id", ondelete="SET NULL")), schema=S)  # fmt: skip
    op.add_column("eo_enrollments", sa.Column("provider_id", sa.Text()), schema=S)  # the lead's LinkedIn or Instagram id at Unipile
    op.add_column("eo_enrollments", sa.Column("chat_id", sa.Text()), schema=S)
    op.create_index("eo_enrollments_social_account_id_idx", "eo_enrollments", ["social_account_id"], schema=S)
    op.add_column("eo_step_runs", sa.Column("social_account_id", sa.Uuid(),
                                            sa.ForeignKey(f"{S}.eo_social_accounts.id", ondelete="SET NULL")), schema=S)  # fmt: skip
    op.add_column("eo_step_runs", sa.Column("action", sa.Text()), schema=S)  # invite | message, for social limits
    op.create_index("eo_step_runs_social_usage_idx", "eo_step_runs", ["social_account_id", "ran_at"], schema=S)
    op.drop_constraint(op.f("eo_prompts_kind_check"), "eo_prompts", schema=S, type_="check")
    op.create_check_constraint(op.f("eo_prompts_kind_check"), "eo_prompts", NEW_KINDS, schema=S)
    # "written": a LinkedIn or Instagram message Coldbrew wrote ahead (in a preview), kept so the same text goes out.
    op.drop_constraint(op.f(OLD_RUN_CHECK), "eo_step_runs", schema=S, type_="check")
    op.create_check_constraint(op.f("eo_step_runs_status_check"), "eo_step_runs", "status in ('sent', 'failed', 'skipped', 'dry_run', 'written')", schema=S)


def downgrade():
    op.execute(f"delete from {S}.eo_step_runs where status = 'written'")
    op.drop_constraint(op.f("eo_step_runs_status_check"), "eo_step_runs", schema=S, type_="check")
    op.create_check_constraint(op.f(OLD_RUN_CHECK), "eo_step_runs", "status in ('sent', 'failed', 'skipped', 'dry_run')", schema=S)
    op.drop_constraint(op.f("eo_prompts_kind_check"), "eo_prompts", schema=S, type_="check")
    op.create_check_constraint(op.f("eo_prompts_kind_check"), "eo_prompts", OLD_KINDS, schema=S)
    op.drop_index("eo_step_runs_social_usage_idx", table_name="eo_step_runs", schema=S)
    op.drop_column("eo_step_runs", "action", schema=S)
    op.drop_column("eo_step_runs", "social_account_id", schema=S)
    op.drop_index("eo_enrollments_social_account_id_idx", table_name="eo_enrollments", schema=S)
    op.drop_column("eo_enrollments", "chat_id", schema=S)
    op.drop_column("eo_enrollments", "provider_id", schema=S)
    op.drop_column("eo_enrollments", "social_account_id", schema=S)
    op.drop_constraint(op.f("eo_sequence_steps_action_check"), "eo_sequence_steps", schema=S)
    op.drop_column("eo_sequence_steps", "action", schema=S)
    op.drop_constraint(op.f("eo_sequences_channel_check"), "eo_sequences", schema=S)
    op.drop_column("eo_sequences", "channel", schema=S)
