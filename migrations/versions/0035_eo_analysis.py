"""Saved outreach analysis snapshots and editable analyst prompt."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None
OLD = "kind in ('verify', 'hook', 'email', 'score', 'followup2', 'followup3', 'followup4', 'followup5', 'sort', 'sort_people', 'product')"
NEW = OLD[:-1]+", 'analysis')"


def upgrade():
    op.create_index('eo_emails_analysis_cohort_idx','eo_emails',['workspace_id','step','sent_at'],schema='outreach')
    op.create_index('eo_send_events_analysis_lead_idx','eo_send_events',['workspace_id','lead_id','occurred_at'],schema='outreach')
    op.create_index('eo_send_events_analysis_email_idx','eo_send_events',['workspace_id','email_id','occurred_at'],schema='outreach')
    op.create_table('eo_analysis_reports',
        sa.Column('id',sa.Uuid(),primary_key=True,server_default=sa.text('gen_random_uuid()')),
        sa.Column('workspace_id',sa.Uuid(),sa.ForeignKey('outreach.eo_workspaces.id',ondelete='CASCADE'),nullable=False),
        sa.Column('created_by',sa.Uuid(),sa.ForeignKey('outreach.eo_users.id',ondelete='SET NULL')),
        sa.Column('job_id',sa.Uuid(),sa.ForeignKey('outreach.eo_jobs.id',ondelete='SET NULL'),unique=True),
        sa.Column('range_start',sa.DateTime(timezone=True),nullable=False),
        sa.Column('range_end',sa.DateTime(timezone=True),nullable=False),
        sa.Column('filters',JSONB(),nullable=False),sa.Column('payload',JSONB(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),schema='outreach')
    op.create_index('eo_analysis_reports_workspace_id_idx','eo_analysis_reports',['workspace_id'],schema='outreach')
    op.create_index('eo_analysis_reports_history_idx','eo_analysis_reports',['workspace_id','created_at'],schema='outreach')
    op.drop_constraint(op.f('eo_prompts_kind_check'),'eo_prompts',schema='outreach',type_='check')
    op.create_check_constraint(op.f('eo_prompts_kind_check'),'eo_prompts',NEW,schema='outreach')


def downgrade():
    # Refuse while analyst prompts remain rather than silently delete edited prompt history.
    op.drop_constraint(op.f('eo_prompts_kind_check'),'eo_prompts',schema='outreach',type_='check')
    op.create_check_constraint(op.f('eo_prompts_kind_check'),'eo_prompts',OLD,schema='outreach')
    op.drop_table('eo_analysis_reports',schema='outreach')
    op.drop_index('eo_send_events_analysis_email_idx',table_name='eo_send_events',schema='outreach')
    op.drop_index('eo_send_events_analysis_lead_idx',table_name='eo_send_events',schema='outreach')
    op.drop_index('eo_emails_analysis_cohort_idx',table_name='eo_emails',schema='outreach')
