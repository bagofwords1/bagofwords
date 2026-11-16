"""Record the folder and knowledge sources authorized for each run."""

import sqlalchemy as sa
from alembic import op

revision = "folder05"
down_revision = "folder04"
branch_labels = None
depends_on = None


def _applied():
    """Idempotent: the dashboard test corpus replays this revision unstamped
    (tests/fixtures/dashboard_corpus/alembic_at.py), then `upgrade head` runs
    it again."""
    return sa.inspect(op.get_bind()).has_table("folder_report_knowledge")


def upgrade():
    if _applied():
        return
    op.create_table(
        "folder_report_knowledge",
        sa.Column("report_id", sa.String(36), sa.ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("folder_id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("item_id", sa.String(36), primary_key=True),
    )


def downgrade():
    if not _applied():
        return
    op.drop_table("folder_report_knowledge")
