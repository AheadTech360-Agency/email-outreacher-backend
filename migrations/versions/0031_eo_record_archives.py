"""Archive products, companies, and people without deleting data."""
from alembic import op
import sqlalchemy as sa

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("eo_products", "eo_companies", "eo_stakeholders"):
        op.add_column(table, sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()), schema="outreach")


def downgrade():
    for table in ("eo_stakeholders", "eo_companies", "eo_products"):
        op.drop_column(table, "archived", schema="outreach")
