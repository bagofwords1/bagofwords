"""Record every agent each run was given, so agent managers can monitor Auto runs."""

import sqlalchemy as sa
from alembic import op

revision = "exposed01"
down_revision = "worldai01"
branch_labels = depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("execution_exposed_agents"):
        return
    op.create_table(
        "execution_exposed_agents",
        sa.Column(
            "execution_id", sa.String(36), sa.ForeignKey("agent_executions.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("agent_id", sa.String(36), primary_key=True),
    )
    op.create_index("ix_execution_exposed_agents_agent", "execution_exposed_agents", ["agent_id", "execution_id"])


def downgrade():
    op.drop_table("execution_exposed_agents")
