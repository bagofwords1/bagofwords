"""Preserve the workspace content hold across connection removal."""
from alembic import op
import sqlalchemy as sa

revision = "fabhold01"
down_revision = "fabrls01"
branch_labels = None
depends_on = None


def upgrade():
    columns = sa.inspect(op.get_bind()).get_columns("organizations")
    if not any(c["name"] == "withhold_unverified_company_content" for c in columns):
        op.add_column("organizations", sa.Column(
            "withhold_unverified_company_content", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE organizations SET withhold_unverified_company_content = TRUE "
               "WHERE id IN (SELECT organization_id FROM connections WHERE active_company_policy IS NOT NULL)")


def downgrade():
    op.drop_column("organizations", "withhold_unverified_company_content")
