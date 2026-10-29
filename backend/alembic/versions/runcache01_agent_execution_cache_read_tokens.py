"""Cache-read tokens on the agent_executions rollup.

Filled by ``app.services.diagnosis.rollup``; ROLLUP_VERSION 2 makes the
startup sweep re-index existing runs, so this migration only adds the column.

Revision ID: runcache01
Revises: emailclaim01
"""

import sqlalchemy as sa

from alembic import op

revision = "runcache01"
down_revision = "emailclaim01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_executions") as batch_op:
        batch_op.add_column(sa.Column("cache_read_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_executions") as batch_op:
        batch_op.drop_column("cache_read_tokens")
