"""Editable product suggestions prompt kind."""
from alembic import op
revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None
OLD = "kind in ('verify', 'hook', 'email', 'score', 'followup2', 'followup3', 'followup4', 'followup5', 'sort', 'sort_people')"
NEW = OLD[:-1] + ", 'product')"


def upgrade():
    op.drop_constraint(op.f("eo_prompts_kind_check"), "eo_prompts", schema="outreach", type_="check")
    op.create_check_constraint(op.f("eo_prompts_kind_check"), "eo_prompts", NEW, schema="outreach")


def downgrade():
    # Refuse downgrade while product prompts remain, rather than deleting edited prompt history.
    op.drop_constraint(op.f("eo_prompts_kind_check"), "eo_prompts", schema="outreach", type_="check")
    op.create_check_constraint(op.f("eo_prompts_kind_check"), "eo_prompts", OLD, schema="outreach")
