"""Record the folder and knowledge sources authorized for each run."""

import sqlalchemy as sa
from alembic import op

revision = "folder05"
down_revision = "folder04"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "folder_report_knowledge",
        sa.Column("report_id", sa.String(36), sa.ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("folder_id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("item_id", sa.String(36), primary_key=True),
    )


def downgrade():
    op.drop_table("folder_report_knowledge")
