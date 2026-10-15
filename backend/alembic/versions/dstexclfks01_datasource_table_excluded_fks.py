"""Per-agent relationship visibility: DataSourceTable.excluded_fks.

A deny-list of foreign-key edges ({column, references_name,
references_column}) the agent manager hid from this agent's schema context.
NULL / empty means every relationship is visible. Kept off ``fks`` because
schema re-sync rewrites that list from the connection.
"""

import sqlalchemy as sa

from alembic import op

revision = "dstexclfks01"
down_revision = "mrgheads10"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("datasource_tables", sa.Column("excluded_fks", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("datasource_tables", "excluded_fks")
