"""Product custom fields and editable notes."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade():
    for column in ("custom_fields", "notes"):
        op.add_column("eo_products", sa.Column(column, JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")), schema="outreach")


def downgrade():
    for column in ("notes", "custom_fields"):
        op.drop_column("eo_products", column, schema="outreach")
