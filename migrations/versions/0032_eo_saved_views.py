"""Personal and shared workspace filter views."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("eo_saved_views",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("outreach.eo_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("outreach.eo_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page", sa.Text(), nullable=False),
        sa.Column("visibility", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("filters", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("visibility in ('personal', 'workspace')", name="eo_saved_views_visibility_check"),
        schema="outreach")
    op.create_index("eo_saved_views_lookup_idx", "eo_saved_views", ["workspace_id", "page", "visibility", "user_id"], schema="outreach")


def downgrade():
    op.drop_table("eo_saved_views", schema="outreach")
