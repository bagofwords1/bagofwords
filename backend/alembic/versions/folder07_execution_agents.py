"""Record the folder selected for each execution and the agents used in it."""

import sqlalchemy as sa
from alembic import op

revision = "folder07"
down_revision = "folder06"
branch_labels = depends_on = None


def _applied():
    """Idempotent: the dashboard test corpus replays this revision unstamped
    (tests/fixtures/dashboard_corpus/alembic_at.py), then `upgrade head` runs
    it again."""
    return sa.inspect(op.get_bind()).has_table("folder_execution_agents")


def upgrade():
    if _applied():
        return
    op.add_column("agent_executions", sa.Column("starting_folder_id", sa.String(36)))
    op.create_table(
        "folder_execution_agents",
        sa.Column(
            "execution_id", sa.String(36), sa.ForeignKey("agent_executions.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("agent_id", sa.String(36), primary_key=True),
    )
    op.create_index("ix_folder_execution_agents_agent", "folder_execution_agents", ["agent_id", "execution_id"])


def downgrade():
    if not _applied():
        return
    op.drop_table("folder_execution_agents")
    op.drop_column("agent_executions", "starting_folder_id")
