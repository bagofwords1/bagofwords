"""Keep a conservative union of agent scopes used by folder-scoped reports."""

from alembic import op
import sqlalchemy as sa

revision = "folder03"
down_revision = "folder02"
branch_labels = None
depends_on = None


def upgrade():
    if "eval_target_ids" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("reports")}:
        op.add_column("reports", sa.Column("eval_target_ids", sa.JSON()))
    if not sa.inspect(op.get_bind()).has_table("folder_report_sources"):
        op.create_table(
            "folder_report_sources",
            sa.Column("report_id", sa.String(36), sa.ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("agent_id", sa.String(36), primary_key=True),
        )


def downgrade():
    op.drop_column("reports", "eval_target_ids")
    op.drop_table("folder_report_sources")
