"""Per-agent column visibility: DataSourceTable.excluded_columns.

A deny-list of column names the agent manager hid from this agent's schema
context. NULL / empty means every column is visible. Kept off ``columns``
because schema re-sync rewrites that list from the connection.
"""

import sqlalchemy as sa

from alembic import op

revision = "dstexclcols01"
down_revision = "mrgheads06"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("datasource_tables", sa.Column("excluded_columns", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("datasource_tables", "excluded_columns")
