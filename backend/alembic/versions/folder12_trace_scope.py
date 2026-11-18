"""Record all starting folders on new executions."""
from alembic import op
import sqlalchemy as sa

revision = "folder12"
down_revision = "folder11"
branch_labels = depends_on = None


def upgrade():
    if "starting_folder_ids" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("agent_executions")}:
        return
    op.add_column("agent_executions", sa.Column("starting_folder_ids", sa.JSON(), nullable=True))


def downgrade():
    if "starting_folder_ids" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("agent_executions")}:
        return
    op.drop_column("agent_executions", "starting_folder_ids")
