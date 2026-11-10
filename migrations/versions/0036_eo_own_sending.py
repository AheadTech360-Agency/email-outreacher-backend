"""Coldbrew's own sending, replacing CheapSequencer step by step: personas, sender accounts (credentials in Supabase
Vault; only the secret's id is kept here), daily usage, sequences and their steps, enrollments, each step's send, and
inbound events (replies, bounces, unsubscribes) with a trigger that stops every enrollment of a lead who replied,
bounced or unsubscribed.

Nothing here touches the CheapSequencer tables or data (eo_emails, eo_send_events, eo_icp_campaigns...).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

S = "outreach"


def _id():
    return sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _ws():
    return sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_workspaces.id", ondelete="CASCADE"), nullable=False)


def _at(name="created_at"):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade():
    # Who an email is from: the name and signature a reader sees. One persona can send from several accounts.
    op.create_table(
        "eo_personas", _id(), _ws(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("signature", sa.Text(), nullable=False, server_default=""),
        _at(), _at("updated_at"),
        sa.UniqueConstraint("workspace_id", "name", name="eo_personas_workspace_id_name_key"),
        schema=S,
    )  # fmt: skip
    op.create_index("eo_personas_workspace_id_idx", "eo_personas", ["workspace_id"], schema=S)

    # A mailbox Coldbrew sends from and reads replies in. Passwords live in Supabase Vault (vault.secrets); only the
    # secret ids are here, and they're never sent to a browser.
    op.create_table(
        "eo_sender_accounts", _id(), _ws(),
        sa.Column("persona_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_personas.id", ondelete="SET NULL")),
        sa.Column("channel", sa.Text(), nullable=False, server_default="email"),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("from_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("from_email", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False, server_default="custom"),
        sa.Column("smtp_host", sa.Text(), nullable=False),
        sa.Column("smtp_port", sa.Integer(), nullable=False),
        sa.Column("smtp_security", sa.Text(), nullable=False),
        sa.Column("smtp_username", sa.Text(), nullable=False),
        sa.Column("smtp_secret_id", sa.Uuid()),
        sa.Column("imap_host", sa.Text()),
        sa.Column("imap_port", sa.Integer()),
        sa.Column("imap_security", sa.Text()),
        sa.Column("imap_username", sa.Text()),
        sa.Column("imap_secret_id", sa.Uuid()),
        sa.Column("daily_limit", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("status", sa.Text(), nullable=False, server_default="untested"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey(f"{S}.eo_users.id", ondelete="SET NULL")),
        _at(), _at("updated_at"),
        sa.CheckConstraint("channel in ('email')", name="eo_sender_accounts_channel_check"),
        sa.CheckConstraint("smtp_security in ('ssl', 'starttls')", name="eo_sender_accounts_smtp_security_check"),
        sa.CheckConstraint("imap_security is null or imap_security in ('ssl', 'starttls')", name="eo_sender_accounts_imap_security_check"),
        sa.CheckConstraint("status in ('untested', 'ok', 'failed', 'paused')", name="eo_sender_accounts_status_check"),
        sa.CheckConstraint("daily_limit between 1 and 500", name="eo_sender_accounts_daily_limit_check"),
        sa.UniqueConstraint("workspace_id", "from_email", name="eo_sender_accounts_workspace_id_from_email_key"),
        schema=S,
    )  # fmt: skip
    op.create_index("eo_sender_accounts_workspace_id_idx", "eo_sender_accounts", ["workspace_id"], schema=S)

    # How many each account sent each day, so the daily limit holds whatever restarts.
    op.create_table(
        "eo_sender_usage",
        sa.Column("sender_account_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_sender_accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("sent", sa.Integer(), nullable=False, server_default="0"),
        schema=S,
    )  # fmt: skip

    # A campaign: its steps, sending window (timezone, hours, days) and status.
    op.create_table(
        "eo_sequences", _id(), _ws(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("lead_intel_icp_id", sa.Uuid()),
        sa.Column("schedule", JSONB(), nullable=False, server_default=sa.text(
            """'{"timezone": "America/New_York", "start": "09:00", "end": "17:00", "days": [1, 2, 3, 4, 5, 6]}'::jsonb""")),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey(f"{S}.eo_users.id", ondelete="SET NULL")),
        _at(), _at("updated_at"),
        sa.CheckConstraint("status in ('draft', 'active', 'paused', 'archived')", name="eo_sequences_status_check"),
        schema=S,
    )  # fmt: skip
    op.create_index("eo_sequences_workspace_id_idx", "eo_sequences", ["workspace_id"], schema=S)

    op.create_table(
        "eo_sequence_steps", _id(),
        sa.Column("sequence_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_sequences.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_order", sa.SmallInteger(), nullable=False),
        sa.Column("delay_days", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("thread_mode", sa.Text(), nullable=False, server_default="reply"),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        _at(), _at("updated_at"),
        sa.CheckConstraint("thread_mode in ('new', 'reply')", name="eo_sequence_steps_thread_mode_check"),
        sa.CheckConstraint("delay_days between 0 and 90", name="eo_sequence_steps_delay_days_check"),
        sa.UniqueConstraint("sequence_id", "step_order", name="eo_sequence_steps_sequence_id_step_order_key"),
        schema=S,
    )  # fmt: skip

    # A lead in a sequence: which account sends to them, which step is next and when, and why it stopped.
    op.create_table(
        "eo_enrollments", _id(), _ws(),
        sa.Column("sequence_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_sequences.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lead_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_account_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_sender_accounts.id", ondelete="SET NULL")),
        sa.Column("email_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_emails.id", ondelete="SET NULL")),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("stop_reason", sa.Text()),
        sa.Column("next_step_order", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("thread_message_id", sa.Text()),
        _at(), _at("updated_at"),
        sa.CheckConstraint("status in ('active', 'paused', 'completed', 'stopped')", name="eo_enrollments_status_check"),
        sa.UniqueConstraint("sequence_id", "lead_id", name="eo_enrollments_sequence_id_lead_id_key"),
        schema=S,
    )  # fmt: skip
    op.create_index("eo_enrollments_workspace_id_idx", "eo_enrollments", ["workspace_id"], schema=S)
    op.create_index("eo_enrollments_due_idx", "eo_enrollments", ["status", "next_run_at"], schema=S)
    op.create_index("eo_enrollments_lead_id_idx", "eo_enrollments", ["lead_id"], schema=S)

    # Each attempt to send a step: sent (with its Message-ID, for threading and matching replies), failed, skipped,
    # or a dry run that only logged.
    op.create_table(
        "eo_step_runs", _id(), _ws(),
        sa.Column("enrollment_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_enrollments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_sequence_steps.id", ondelete="SET NULL")),
        sa.Column("sender_account_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_sender_accounts.id", ondelete="SET NULL")),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text()),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text()),
        _at("ran_at"),
        sa.CheckConstraint("status in ('sent', 'failed', 'skipped', 'dry_run')", name="eo_step_runs_status_check"),
        schema=S,
    )  # fmt: skip
    op.create_index("eo_step_runs_enrollment_id_idx", "eo_step_runs", ["enrollment_id"], schema=S)
    op.create_index("eo_step_runs_message_id_idx", "eo_step_runs", ["message_id"], schema=S)

    # What the inbox reader found: a reply, bounce, unsubscribe or auto-reply, matched to a lead where it could be.
    op.create_table(
        "eo_inbound_events", _id(), _ws(),
        sa.Column("sender_account_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_sender_accounts.id", ondelete="SET NULL")),
        sa.Column("enrollment_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_enrollments.id", ondelete="SET NULL")),
        sa.Column("lead_id", sa.Uuid(), sa.ForeignKey(f"{S}.eo_leads.id", ondelete="SET NULL")),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("in_reply_to", sa.Text()),
        sa.Column("from_email", sa.Text(), nullable=False, server_default=""),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("snippet", sa.Text(), nullable=False, server_default=""),
        sa.Column("folder", sa.Text(), nullable=False, server_default="INBOX"),
        sa.Column("raw", JSONB()),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        _at(),
        sa.CheckConstraint("type in ('reply', 'bounce', 'unsubscribe', 'auto_reply')", name="eo_inbound_events_type_check"),
        sa.UniqueConstraint("sender_account_id", "message_id", name="eo_inbound_events_sender_account_id_message_id_key"),
        schema=S,
    )  # fmt: skip
    op.create_index("eo_inbound_events_workspace_id_idx", "eo_inbound_events", ["workspace_id"], schema=S)
    op.create_index("eo_inbound_events_lead_id_idx", "eo_inbound_events", ["lead_id"], schema=S)

    # A reply, bounce or unsubscribe stops every active or paused enrollment of that lead, in every sequence, in the
    # same transaction that records it: nothing more can be scheduled for them, whatever the scheduler is doing.
    op.execute(f"""
        create or replace function {S}.eo_stop_enrollments_on_inbound() returns trigger
        language plpgsql set search_path = '' as $$
        begin
          if new.lead_id is not null and new.type in ('reply', 'bounce', 'unsubscribe') then
            update {S}.eo_enrollments
               set status = 'stopped',
                   stop_reason = case new.type when 'reply' then 'replied' when 'bounce' then 'bounced' else 'unsubscribed' end,
                   next_run_at = null,
                   updated_at = now()
             where lead_id = new.lead_id and status in ('active', 'paused');
          end if;
          return new;
        end $$;
    """)
    op.execute(f"""
        create trigger eo_inbound_events_stop_enrollments after insert on {S}.eo_inbound_events
        for each row execute function {S}.eo_stop_enrollments_on_inbound();
    """)


def downgrade():
    op.execute(f"drop trigger if exists eo_inbound_events_stop_enrollments on {S}.eo_inbound_events")
    op.execute(f"drop function if exists {S}.eo_stop_enrollments_on_inbound()")
    for table in ("eo_inbound_events", "eo_step_runs", "eo_enrollments", "eo_sequence_steps", "eo_sequences",
                  "eo_sender_usage", "eo_sender_accounts", "eo_personas"):
        op.drop_table(table, schema=S)
    # The Vault secrets of deleted sender accounts are removed by the app when an account is deleted; a downgrade
    # leaves vault.secrets alone (it's not ours to bulk-delete).
