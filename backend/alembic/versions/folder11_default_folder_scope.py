"""Remember personal folder scope on each workspace membership."""
from alembic import op
import sqlalchemy as sa

revision = "folder11"
down_revision = "folder10"
branch_labels = depends_on = None


def upgrade():
    op.add_column("memberships", sa.Column("default_agent_folder_ids", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("memberships", "default_agent_folder_ids")
