"""Persist report folder selection and the original targets of each eval result."""

from alembic import op
import sqlalchemy as sa

revision = "folder02"
down_revision = "folder01"
branch_labels = None
depends_on = None


def upgrade():
    for table, column in (
        ("reports", sa.Column("agent_folder_id", sa.String(36))),
        ("test_results", sa.Column("data_source_ids_json", sa.JSON())),
    ):
        if column.name not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}:
            op.add_column(table, column)


def downgrade():
    op.drop_column("test_results", "data_source_ids_json")
    op.drop_column("reports", "agent_folder_id")
