"""Additional selected folders on a report, alongside its original folder."""

import sqlalchemy as sa
from alembic import op

revision = "folder08"
down_revision = "folder07"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "report_additional_folders",
        sa.Column("report_id", sa.String(36), sa.ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("folder_id", sa.String(36), primary_key=True),
    )
    # Older attachment flows kept global scope even after a folder was selected.
    op.execute("UPDATE instructions SET scope_mode='targeted' WHERE scope_mode='global' "
               "AND id IN (SELECT instruction_id FROM folder_instructions)")
    op.execute("UPDATE instruction_versions SET scope_mode='targeted' WHERE id IN "
               "(SELECT current_version_id FROM instructions WHERE id IN "
               "(SELECT instruction_id FROM folder_instructions))")


def downgrade():
    op.drop_table("report_additional_folders")
